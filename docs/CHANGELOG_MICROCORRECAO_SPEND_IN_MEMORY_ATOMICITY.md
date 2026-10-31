# CHANGELOG — Microcorreção BF2: Spend In-Memory Atomicity

Base: `MULTIVERSO_GROWTH_STUDIO_MASTER_V1_EXPANSION_V6_1_1_BF2_SPEND_CROSS_INSTANCE`
(1493 testes). Corrige a falha real reproduzida pela auditoria no
caminho em memória (`db_path=None`), preservando tudo já aprovado
(incluindo todos os testes SQLite cross-instance/process da correção
anterior).

## Resultado real — 3 execuções consecutivas, verificação máxima

```
COLLECTED: 1499  PASS: 1499  FAIL: 0  ERROR: 0  SKIP: 0
DURATION: ~76-78s (3 execuções consecutivas, -W error::ResourceWarning)
EXIT CODE: 0 (as 3 vezes)
Arquivos órfãos: 0
```

**1499 testes** (1493 preservados + **6 testes novos**, cada um
repetindo a verificação 15 vezes internamente — 90 rodadas de stress
no total).

## Falha real confirmada

`SpendGuard` com `db_path=None`, sob concorrência de threads na MESMA
instância, podia criar múltiplos `approval_id` para uma única
escalação aprovada — reprodução independente reportada: 1 aprovação
de R$100 gerou 3 `approval_id` e `total_approved_budget=R$300`.

## Causa raiz encontrada por revisão de código

`confirm_approval()` fazia duas coisas em DUAS aquisições de lock
SEPARADAS:
1. `_confirm_approval_memory()`: checava se já existia aprovação,
   criava uma nova SE necessário, e **retornava** — tudo isso dentro
   de UMA aquisição de `self._spend_lock`, mas **sem registrar** a
   nova aprovação em `self._approvals`/`self._escalation_to_approval`
   antes de liberar o lock.
2. De volta em `confirm_approval()` (método chamador): SÓ ENTÃO,numa
   **segunda aquisição separada** do mesmo lock, registrava a
   aprovação recém-criada nos dois dicionários.

Entre o fim do passo 1 e o início do passo 2, o lock estava
LIBERADO — uma janela onde OUTRAS threads podiam entrar em
`_confirm_approval_memory()`, checar `_escalation_to_approval` (ainda
vazio, pois a primeira thread não tinha registrado nada ainda), e
também criar sua PRÓPRIA aprovação nova. Múltiplas threads podiam
assim "vencer" a checagem "ainda não existe" antes de qualquer uma
registrar — resultando em múltiplos `approval_id` para a mesma
escalação.

Não consegui reproduzir a falha de forma confiável neste ambiente
específico (o GIL do CPython torna a janela estreita o suficiente
para raramente se manifestar aqui, mesmo com 50-100 threads e 30
rodadas de tentativa) — mas a revisão de código identificou a causa
de forma conclusiva e inequívoca, e a correção foi aplicada e
validada independentemente da capacidade de reproduzir o sintoma
neste ambiente particular.

## Correção aplicada

Para o caminho em memória, TODA a sequência —checar existente, criar
se necessário, registrar em `_approvals`, registrar em
`_escalation_to_approval` — agora acontece inteiramente dentro de
UMA ÚNICA aquisição de `self._spend_lock`, em
`_confirm_approval_memory()`. Nenhuma janela entre "criar" e
"registrar" permanece. O método chamador `confirm_approval()` não
faz mais nenhuma segunda aquisição de lock para o caminho em memória
— apenas delega e devolve o resultado já completo.

`record_spend()` também foi revisado e simplificado — a
reatribuição redundante fora da aquisição de lock (inofensiva, mas
desnecessária e um risco de reintroduzir um bug similar numa futura
alteração) foi removida para o caminho em memória; `_record_spend_memory()`
já fazia checagem+débito atomicamente dentro de uma única aquisição
de lock.

O caminho com `db_path` configurado (SQLite, corrigido na rodada
anterior) permanece inalterado — sua atomicidade vem do próprio banco
(`INSERT` + `UNIQUE` + `IntegrityError`), não do lock Python.

## Validado com estresse agressivo e repetido

- **2, 10, 50 e 100 threads** confirmando concorrentemente a mesma
  escalação, cada cenário repetido **15 vezes** — sempre exatamente 1
  `approval_id`, orçamento sempre R$100 (nenhuma das 60 rodadas
  falhou).
- **Gasto concorrente**: 30 threads tentando gastar R$10 cada (R$300
  no total) sobre uma aprovação de R$100, repetido 15 vezes — sempre
  exatamente 10 sucessos (R$100 exato), nunca mais.
- **Cenário R$80+R$80 sobre R$100** (mesmo do teste cross-instance),
  agora em memória — repetido 15 vezes — sempre exatamente 1
  sucesso, gasto final sempre R$80.

## Preservação confirmada

Reexecutei explicitamente `tests/test_microcorrecao_spend_cross_instance_atomicity.py`
(a suíte SQLite cross-instance/process da correção anterior) — todos
os 8 testes continuam passando sem nenhuma alteração, confirmando que
a correção do caminho em memória não afetou o caminho SQLite.

## Arquivos alterados
- `bridge/execution/spend_guard.py` — `confirm_approval()` e
  `_confirm_approval_memory()` reestruturados para uma única seção
  crítica atômica; `record_spend()` simplificado

## Arquivos novos
- `tests/test_microcorrecao_spend_in_memory_atomicity.py` (6 testes,
  90 rodadas de stress no total)

**Nenhuma funcionalidade já aprovada foi removida.** A API pública
(`confirm_approval`/`record_spend`/`total_approved_budget`/
`approval_for_escalation`) permanece idêntica; todos os testes
anteriores (SQLite cross-instance e em memória single-thread)
continuam passando sem alteração.

## Pendências reais (honestamente declaradas)
- Não consegui reproduzir o sintoma exato da falha neste ambiente
  (janela de corrida estreita sob o GIL do CPython) — a correção foi
  validada por revisão de código conclusiva + testes de estresse
  extensivos (60+ rodadas), não por reprodução direta do sintoma
  original
- `pytest` continua indisponível neste ambiente
