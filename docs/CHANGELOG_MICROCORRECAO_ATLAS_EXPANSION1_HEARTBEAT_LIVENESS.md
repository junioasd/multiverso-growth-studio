# CHANGELOG — Microcorreção ATLAS EXPANSION 1: Liveness Real (Heartbeat) para GATE_IN_PROGRESS

Base: `MULTIVERSO_GROWTH_STUDIO_MASTER_V1_EXPANSION_V6_1_1_ATLAS_EXPANSION1_CRASH_RECOVERY`
(1691 testes). Corrige a falha real reproduzida pela auditoria,
preservando integralmente V1 + demais correções da Expansion 1.

## Resultado real — 3 execuções consecutivas, verificação máxima

```
COLLECTED: 1694  PASS: 1694  FAIL: 0  ERROR: 0  SKIP: 0
DURATION: ~104-106s (3 execuções consecutivas, -W error::ResourceWarning)
EXIT CODE: 0 (as 3 vezes)
ResourceWarnings: 0, threads residuais: 0, subprocessos órfãos: 0
compileall: OK
```

**1694 testes** (1691 preservados + **3 testes novos** + 1 asserção
de teste existente corrigida, ver abaixo).

## Falha real confirmada

A correção anterior (TTL + fencing generation) resolveu a
recuperação de CRASH real, mas introduziu uma lacuna distinta:
`GATE_IN_PROGRESS` expirava apenas por `leased_at` ESTÁTICO — sem
heartbeat/renovação, um Quality Gate SAUDÁVEL mas legitimamente mais
lento que `gate_in_progress_ttl_seconds` fazia OUTRA instância
considerar o lease morto, reclamar, e avaliar o MESMO gate
simultaneamente. Reproduzido: `runner` 1x, gate 2x, DUAS chamadas
retornando `VERIFIED`.

## Correção — heartbeat atômico condicionado ao token atual

Novo `_evaluate_quality_gate_with_heartbeat()`: enquanto a chamada
(síncrona e bloqueante) ao `quality_gate_checker` está em andamento,
uma thread de heartbeat em segundo plano renova `leased_at`
periodicamente — a cada `gate_in_progress_ttl_seconds / 3` (nunca
mais devagar que um terço do TTL) — via novo `_renew_lease_heartbeat()`.

A renovação é ATÔMICA e condicionada ao `lease_token` ATUAL:
`UPDATE ... WHERE lease_token = ? AND status = 'GATE_IN_PROGRESS'`
— se o token não bater mais (outra instância já reclamou — nunca
deveria acontecer enquanto o heartbeat roda corretamente, mas seria
o caso de um crash real seguido de reclaim), a renovação é um no-op
silencioso, NUNCA sobrescrevendo um lease que outra instância já
assumiu.

Um checker genuinamente saudável, mesmo que demore MUITO mais que o
TTL, nunca faz o lease parecer morto para outra instância — reclaim
só ocorre de verdade quando o heartbeat CESSA (crash real, sem
exceção tratável, sem thread de heartbeat rodando — ela é do MESMO
processo e morre junto) e o TTL se esgota sem nenhuma renovação.

Aplicado apenas ao caminho de outcome TARDIO
(`_finalize_pending_gate_outcome`), onde o lease genuinamente sits
em `GATE_IN_PROGRESS` durante a avaliação — o caminho de execução
FRESCA (`_run_exclusive_retry_loop`) nunca transiciona para esse
status durante a avaliação do gate (permanece `IN_PROGRESS`, que
nunca é elegível para reclaim por TTL), então não tem essa exposição.

## Ajuste necessário num teste pré-existente (comportamento correto, nunca alterado)

`test_two_instances_racing_to_claim_pending_gate_outcome_never_rerun_task_runner`
(rodada anterior) assumia que o "perdedor" de uma corrida pela
reivindicação inicial sempre encontraria a missão já `VERIFIED`
(caminho rápido de retomada) — mas `TIMED_OUT_UNKNOWN_OUTCOME` (o
perdedor observando `GATE_IN_PROGRESS` ainda dentro do TTL) sempre
foi um desfecho igualmente CORRETO, já estabelecido em outro teste
da mesma série
(`test_loser_of_claim_race_observes_gate_in_progress_never_evaluates`).
A pequena sobrecarga de criar a thread de heartbeat tornou esse
desfecho consistentemente observável nesse teste específico (antes,
coincidentemente raro o bastante para nunca aparecer). Nenhum
COMPORTAMENTO foi alterado — apenas a asserção do teste foi
corrigida para aceitar o conjunto completo de desfechos corretos já
documentados em outros testes desta mesma série.

## Validado com os cenários exatos exigidos

`tests/test_microcorrecao_atlas_expansion1_heartbeat_liveness.py` (3
testes):
- **Cenário exato**: checker deliberadamente lento (4× o TTL), mas
  SAUDÁVEL — `runner` 1x, gate 1x, nenhuma segunda instância faz
  reclaim enquanto houver heartbeat.
- Prova POSITIVA direta via SQL: `leased_at` confirmado avançando
  genuinamente ao longo de múltiplas leituras enquanto o gate lento
  está rodando.
- **Reconfirmação do teste de crash real** (preservado explicitamente
  nesta rodada, conforme exigido): subprocesso real reivindica o
  gate e morre com `os._exit()` — a thread de heartbeat, sendo do
  MESMO processo, morre junto; após o TTL expirar sem nenhuma
  renovação, uma instância saudável faz reclaim e finaliza
  corretamente — `runner` ainda exatamente 1x.
- Confirmado sem flakiness rodando o cenário completo 5× seguidas.

## Preservação confirmada explicitamente

Reexecutei os 25 arquivos de teste dedicados de TODAS as rodadas
anteriores — **295 testes, todos passando sem alteração** (exceto a
única asserção corrigida, documentada acima — comportamento
inalterado), incluindo os 82 testes das sete rodadas anteriores de
ATLAS EXPANSION 1.

## Arquivos alterados
- `atlas/orchestration/graph_executor.py` — novo
  `_renew_lease_heartbeat()`; novo
  `_evaluate_quality_gate_with_heartbeat()`;
  `_finalize_pending_gate_outcome()` usa o wrapper com heartbeat em
  vez da chamada direta
- `tests/test_microcorrecao_atlas_expansion1_delayed_gate.py` —
  asserção de `test_two_instances_racing_to_claim_pending_gate_outcome_never_rerun_task_runner`
  ampliada para aceitar `TIMED_OUT_UNKNOWN_OUTCOME` como desfecho
  válido do perdedor da corrida (comportamento já correto e
  documentado alhures, nunca alterado)

## Arquivos novos
- `tests/test_microcorrecao_atlas_expansion1_heartbeat_liveness.py` (3 testes)

## Pendências reais (honestamente declaradas)
- `pytest` continua indisponível neste ambiente
- O intervalo de heartbeat (`TTL / 3`) é uma heurística — um
  ambiente com latência de escrita SQLite extremamente alta (bem
  acima do intervalo) poderia, em teoria, ainda perder uma renovação
  a tempo; isso é uma limitação inerente de qualquer mecanismo de
  heartbeat baseado em tempo, não específica desta implementação. O
  responsável pela integração deve calibrar
  `gate_in_progress_ttl_seconds` considerando tanto o tempo máximo
  esperado do `quality_gate_checker` quanto a latência de escrita do
  ambiente de banco
