# CHANGELOG — Microcorreção: Segurança + Execução Reproduzível (Bloco Final 2)

Base: `MULTIVERSO_GROWTH_STUDIO_MASTER_V1_EXPANSION_V6_1_1_BLOCO_FINAL_2`
(1432 testes). Corrige SOMENTE as falhas confirmadas pela auditoria
independente, preservando tudo já aprovado integralmente.

## Resultado real — 3 execuções consecutivas, verificação máxima

```
COLLECTED: 1451  PASS: 1451  FAIL: 0  ERROR: 0  SKIP: 0
DURATION: ~75-77s (3 execuções consecutivas, -W error::ResourceWarning)
EXIT CODE: 0 (as 3 vezes)
Arquivos órfãos: 0
```

**1451 testes** (1432 preservados + **19 testes novos** de regressão
específica desta correção).

## Item 1 — OWNER APPROVAL BINDING ✅

**Falha confirmada**: `NavigatorReal._ensure_owner_approval()` verificava
apenas `status == APPROVED`, nunca o CONTEÚDO da escalação — uma
aprovação concedida para UMA ação/plataforma/canal podia ser
reutilizada (`approved_escalation_id`) para executar QUALQUER outra
ação/plataforma/canal/ator, sem nova aprovação do Owner.

**Corrigido**: todo `execute_action()` crítico agora computa um
`binding_fingerprint` (via `core.governance.idempotency.
stable_fingerprint`, já usado em outras partes do sistema — nunca uma
segunda implementação divergente) cobrindo `platform`+`action`+
`params`+`actor`. Esse fingerprint é persistido no `context` da
escalação no momento da criação. Ao reutilizar um
`approved_escalation_id`, o fingerprint é RECALCULADO a partir dos
parâmetros da chamada atual e comparado EXATAMENTE contra o
persistido — qualquer diferença (canal, ação, plataforma, ator)
rejeita a execução com `ApprovalContextMismatch`, mesmo com a
escalação `APPROVED`.

Testado explicitamente: reuso com canal diferente, ação diferente,
plataforma diferente, e ator diferente — todos rejeitados; contexto
exato continua funcionando.

## Item 2 — SPEND GUARD ✅

**3 falhas confirmadas e corrigidas**:

1. **`confirm_approval()` nunca validava o contexto**: aceitava
   `platform`/`campaign_reference`/`currency` quaisquer, sem checar
   contra o que foi realmente aprovado na escalação — permitia
   "trocar" a campanha/plataforma/moeda depois de aprovado. Corrigido:
   comparação EXATA contra `item.context` armazenado; qualquer
   divergência levanta `SpendContextMismatch`.
2. **`record_spend()` nunca rejeitava `amount <= 0`**: um gasto
   negativo diminuía `spent_so_far`, inflando artificialmente
   `remaining()` — um mecanismo de "devolver"/criar orçamento do
   nada. Corrigido: `amount <= 0` sempre levanta `ValueError`
   explícito, antes de qualquer outra lógica.
3. **Sem atomicidade**: `record_spend()` fazia checar+debitar em dois
   passos não-atômicos — uma corrida de concorrência genuína podia
   permitir overspend coletivo além do aprovado. Corrigido: um
   `threading.Lock` envolve TODA a operação de checar+debitar.
   **Testado com 30 threads reais** tentando gastar R$10 cada (total
   R$300, aprovado R$100) — resultado final sempre exatamente R$100,
   nunca mais, confirmado repetidamente.

## Item 3 — DEPENDÊNCIAS / INSTALAÇÃO LIMPA ✅

`requirements.txt` estava completamente desatualizado (ainda afirmava
"100% stdlib", uma alegação de rodadas anteriores nunca revisada).
Corrigido com varredura estática de TODOS os arquivos `.py` do
projeto (AST, não grep) — 8 dependências de terceiros genuinamente
importadas, todas agora listadas com versão mínima testada: Pillow,
pypdf, python-docx, opencv-python, numpy, cryptography, oauthlib,
playwright. `PyJWT` (instalado neste ambiente mas nunca importado
pelo código) foi deliberadamente OMITIDO — nunca lista uma
dependência não usada.

Teste de regressão dedicado (`RequirementsCompletenessRegressionTests`)
reescaneia o código e falha automaticamente se um import de terceiros
futuro não for adicionado ao `requirements.txt`.

## Item 4 — PLAYWRIGHT / BROWSER ✅

- `BrowserExecutor.is_browser_available()` (novo): verifica
  EXPLICITAMENTE, antes de qualquer tentativa de lançamento, se a
  biblioteca está instalada E se o binário do Chromium existe no
  caminho reportado pelo próprio Playwright — nunca afirma
  disponibilidade sem checar o sistema de arquivos real.
- `requirements.txt` documenta o passo OBRIGATÓRIO
  `playwright install chromium` (separado do `pip install`, já que
  os binários não são pacotes pip) — com nota explicando por quê.
- Reformulada a linguagem do módulo: removida a afirmação
  "Chromium funcional" como fato assumido — agora documentado como
  algo que `is_browser_available()` verifica em tempo real, no
  ambiente onde o código efetivamente roda.
- Testado: binário ausente (simulado) → `UNAVAILABLE` limpo, nunca
  exceção crua; binário presente (real, neste ambiente, após o setup
  documentado) → navegação real funciona de ponta a ponta com uma URL
  `data:`.

## Item 5 — REGRESSÃO ✅

Novo `tests/test_microcorrecao_seguranca_bloco_final2.py` — 19 testes
independentes, organizados exatamente pelos 6 cenários pedidos:
reutilização indevida de aprovação (4 variações: canal/ação/
plataforma/ator), gasto negativo/zero, troca de plataforma/campanha/
moeda após aprovação (3 variações), overspend (single + concorrência
real com 30 threads), completude de `requirements.txt` (2 testes,
scan estático real), e binário de navegador (detecção + navegação
real após setup).

## Arquivos alterados
- `bridge/execution/navigator_real.py` — binding de aprovação por
  fingerprint completo
- `bridge/execution/spend_guard.py` — validação de contexto, rejeição
  de gasto negativo/zero, lock de atomicidade
- `bridge/execution/browser_executor.py` — `is_browser_available()`,
  linguagem menos assertiva sobre o ambiente
- `requirements.txt` — reescrito com as 8 dependências reais

## Arquivos novos
- `tests/test_microcorrecao_seguranca_bloco_final2.py` (19 testes)

**Nenhuma funcionalidade já aprovada foi alterada.** 25 agentes
ACTIVE, providers reais, multimodal real, Access & Execution Hub,
Navigator Real, Radar Real, webhooks, observabilidade — tudo
preservado integralmente; os testes antigos de `SpendGuard`/
`NavigatorReal` já usavam contexto consistente e continuam passando
sem alteração.

## Pendências reais (honestamente declaradas)
- Validação completa "instalação em venv verdadeiramente vazio" não é
  possível neste sandbox (sem acesso à rede para `pip install` de
  verdade) — a checagem de completude do `requirements.txt` é
  estática (AST) e automática a cada execução da suíte, mas não
  substitui um teste real de instalação em CI
- `pytest` continua indisponível neste ambiente
