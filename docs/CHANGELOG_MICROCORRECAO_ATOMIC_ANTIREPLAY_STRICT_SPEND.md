# CHANGELOG — Microcorreção BF2: Atomic Anti-Replay + Strict Spend Context

Base: `MULTIVERSO_GROWTH_STUDIO_MASTER_V1_EXPANSION_V6_1_1_BF2_OWNER_GATE_ANTIREPLAY`
(1473 testes). Corrige as 2 falhas reproduzidas independentemente pela
auditoria, preservando tudo já aprovado.

## Resultado real — 3 execuções consecutivas, verificação máxima

```
COLLECTED: 1485  PASS: 1485  FAIL: 0  ERROR: 0  SKIP: 0
DURATION: ~74-77s (3 execuções consecutivas, -W error::ResourceWarning)
EXIT CODE: 0 (as 3 vezes)
Arquivos órfãos: 0
```

**1485 testes** (1473 preservados + **12 testes novos**, todos usando
um **servidor HTTP local real** — nunca `mock.patch` — para a
verificação de atomicidade, a mesma disciplina que a auditoria usou
para encontrar a falha original).

## Item 1 — ANTI-REPLAY ATÔMICO ✅

**Falha reproduzida pela auditoria**: duas execuções concorrentes com
a MESMA `escalation_id` + MESMA `idempotency_key`, usando um servidor
HTTP real, dispararam 2 requisições HTTP reais (o servidor recebeu 2
POSTs). Reproduzi o EXATO cenário antes de corrigir — confirmado.

**Causa raiz**: o padrão anterior era "checar consumo sob lock →
LIBERAR o lock → despachar → só então marcar consumido" — duas
threads passavam pela checagem de "ainda não consumido" ANTES de
qualquer uma marcar o consumo. Um lock que se libera antes do efeito
externo nunca é atômico o suficiente.

**Corrigido com CLAIM atômico via `INSERT`**: nova tabela
`navigator_execution_claims` com `escalation_id` como `PRIMARY KEY`.
SQLite garante que, entre múltiplas transações concorrentes tentando
inserir a MESMA chave primária (mesmo processo com múltiplas threads,
OU processos diferentes compartilhando o mesmo banco), exatamente UMA
consegue committar — as demais recebem `IntegrityError` de forma
determinística. Fluxo:

1. Tentativa de `INSERT ... VALUES (escalation_id, 'CLAIMED', ...)`.
2. Se o INSERT tem sucesso → ganhamos o claim → despachamos a ação
   real.
3. Se falha (`IntegrityError`, linha já existe) → lê o estado:
   - `CONSUMED` + mesma `idempotency_key` → devolve resultado
     CACHEADO (nunca despacha de novo).
   - `CONSUMED` + chave diferente/ausente → rejeita imediatamente.
   - `CLAIMED` (em andamento) + mesma chave → AGUARDA (polling
     limitado, nunca infinito — no máximo 30s) o resultado da
     execução vencedora.
   - `CLAIMED` + chave diferente → rejeita IMEDIATAMENTE, nunca
     espera.
4. Sucesso → `UPDATE ... SET status='CONSUMED'` com o resultado.
5. Falha → `DELETE` da linha (libera o claim) — nunca "queima" a
   aprovação por uma tentativa que não teve efeito externo algum.
6. **Reconciliação de crash**: um claim `CLAIMED` muito mais velho
   (>120s) que qualquer execução real levaria (todas completam em
   segundos) é tratado como abandonado (processo que morreu sem
   chance de liberar) e pode ser reivindicado de novo — testado que
   isso resolve em <5s, nunca trava a aprovação para sempre.

**Validado com servidor HTTP real** (nunca mockado): 2, 10 e 50
threads concorrentes com a mesma `idempotency_key` → sempre
EXATAMENTE 1 requisição HTTP real recebida pelo servidor, todas as
execuções retornam sucesso. Testado também entre DUAS instâncias
diferentes de `NavigatorReal` compartilhando o mesmo banco (20
chamadas concorrentes de 2 "processos" simulados) → ainda exatamente
1 efeito real. Chave diferente → sempre rejeitada, nunca espera, nunca
duplica.

## Item 2 — SPEND CONTEXT ESTRITO ✅

**Falha reproduzida pela auditoria**: após confirmar corretamente uma
aprovação de gasto, uma segunda chamada a `confirm_approval()` com
`platform`/`campaign_reference`/`currency` DIFERENTES foi aceita
silenciosamente, devolvendo a aprovação existente sem validar o novo
contexto.

**Causa raiz**: a checagem "já existe aprovação para esta
escalação?" acontecia ANTES da validação de contexto — uma vez que
uma aprovação existia, o método retornava cedo, pulando toda a
validação.

**Corrigido**: a validação de contexto (contra `item.context`,
sempre o que foi ORIGINALMENTE aprovado pelo Owner — nunca o que o
chamador afirma) agora roda em TODA chamada, incondicionalmente,
ANTES de checar se já existe uma aprovação vinculada. Só depois da
validação passar é que a idempotência (devolver a aprovação
existente) entra em ação. Reproduzido o cenário exato do auditor como
teste permanente — confirmado corrigido.

## Testes obrigatórios — todos cobertos, com servidor real

`tests/test_microcorrecao_atomic_antireplay_strict_spend.py`:
- 2/10/50 threads simultâneas, mesma operação, servidor HTTP real →
  exatamente 1 efeito externo (3 testes)
- Mesma prova entre duas instâncias/processos usando o mesmo DB →
  exatamente 1 efeito externo
- `idempotency_key` diferente → sempre bloqueado, nunca espera
- Restart/crash (claim travado simulado) → reconciliado com
  segurança, nunca replay, nunca trava para sempre
- Reconfirmação de gasto com plataforma/campanha/moeda diferente →
  sempre rejeitada (5 testes, incluindo o cenário exato reportado)
- Regressão completa PASS, 0 hangs, 0 ResourceWarnings, 3 execuções
  consecutivas

Nenhum teste de atomicidade usa `mock.patch` de rede — todos usam um
servidor HTTP local real e genuíno, a mesma disciplina que expôs a
falha original.

## Correção adicional (falso positivo do meu próprio teste)
`RequirementsCompletenessRegressionTests` (da correção anterior)
sinalizou `socketserver` como dependência de terceiros ausente —
`socketserver` é da biblioteca padrão do Python; faltava na lista
interna do próprio teste. Corrigido.

## Arquivos alterados
- `bridge/execution/navigator_real.py` — reescrito: claim atômico via
  SQLite `INSERT`/`PRIMARY KEY`, polling limitado para aguardar
  execução concorrente, reconciliação de claim travado por crash
- `bridge/execution/spend_guard.py` — validação de contexto movida
  para ANTES da checagem de idempotência, roda em toda chamada
- `core/models/db.py` — nova tabela `navigator_execution_claims`
- `tests/test_microcorrecao_seguranca_bloco_final2.py` — `socketserver`
  adicionado à lista de módulos stdlib conhecidos (falso positivo)

## Arquivos novos
- `tests/test_microcorrecao_atomic_antireplay_strict_spend.py` (12 testes)

**Nenhuma funcionalidade já aprovada foi removida.** 25 agentes
ACTIVE, criticidade autoritativa, Owner Approval Binding, providers
reais — tudo preservado integralmente; todos os testes anteriores de
`NavigatorReal`/`SpendGuard` (usando `mock.patch`) continuam passando
sem alteração, validando que o comportamento observável se manteve
correto, apenas agora genuinamente atômico.

## Pendências reais (honestamente declaradas)
- `pytest` continua indisponível neste ambiente
