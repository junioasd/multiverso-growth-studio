# CHANGELOG — Microcorreção ATLAS EXPANSION 1: Unificação do Caminho Fresco com o Mecanismo Durável

Base: `MULTIVERSO_GROWTH_STUDIO_MASTER_V1_EXPANSION_V6_1_1_ATLAS_EXPANSION1_HEARTBEAT_LIVENESS`
(1694 testes). Corrige a falha real reproduzida pela auditoria,
preservando integralmente V1 + demais correções da Expansion 1.

## Resultado real — 3 execuções consecutivas, verificação máxima

```
COLLECTED: 1699  PASS: 1699  FAIL: 0  ERROR: 0  SKIP: 0
DURATION: ~108-110s (3 execuções consecutivas, -W error::ResourceWarning)
EXIT CODE: 0 (as 3 vezes)
ResourceWarnings: 0, threads residuais: 0, subprocessos órfãos: 0
compileall: OK
```

**1699 testes** (1694 preservados + **5 testes novos**).

## Falha real confirmada (reproduzida com subprocesso real)

No caminho de execução FRESCA (quando `task_runner` termina DENTRO
do timeout, nunca um outcome tardio), se havia `Quality Gate`
configurado, o outcome recém-obtido só existia em memória LOCAL até
a avaliação do gate terminar — o lease permanecia em `IN_PROGRESS`.
Reproduzido com um subprocesso real: `runner` conclui, o
`quality_gate_checker` morre o processo via `os._exit()` — o EFEITO
REAL do `task_runner` já tinha ocorrido, mas o outcome nunca fora
persistido. Confirmado: `lease.status == "IN_PROGRESS"` e
`lease.outcome_json IS NULL` após o crash — e, criticamente,
`IN_PROGRESS` NÃO TEM nenhum mecanismo de TTL/reclaim aplicável (só
`GATE_IN_PROGRESS` tem) — o outcome ficava PERDIDO PARA SEMPRE, um
cenário ainda mais severo que o já corrigido para outcomes tardios.

## Correção — unificação real, não um caminho paralelo

Assim que `task_runner` retorna sucesso e há `task.quality_gate`
configurado, novo `_persist_outcome_and_claim_gate()` persiste o
outcome ATOMICAMENTE e transiciona o lease DIRETO para
`GATE_IN_PROGRESS` (mesmo `lease_token`, `generation` avançada) —
ANTES de qualquer avaliação do gate começar. A partir daí, o código
DELEGA diretamente para `_finalize_pending_gate_outcome()` — a MESMA
função já usada e validada para outcomes tardios, reaproveitando
INTEGRALMENTE o mecanismo já existente: claim exclusivo (compare-
and-set), heartbeat (liveness real durante avaliação lenta), TTL +
reclaim atômico (recuperação de crash real), fencing generation.
Nenhum caminho separado, nenhuma lógica duplicada — a antiga lógica
inline de APPROVED/REJECTED/UNAVAILABLE/exceção dentro de
`_run_exclusive_retry_loop()` foi REMOVIDA por completo, substituída
por esta delegação.

Tarefas SEM `quality_gate` configurado continuam pelo caminho direto
e rápido de sempre (`COMPLETED`→`VERIFIED` imediato) — sem a
sobrecarga da unificação, já que não há código externo/arriscado
rodando entre o sucesso do `task_runner` e a finalização nesse caso.

## Validado com o cenário exato exigido

`tests/test_microcorrecao_atlas_expansion1_fresh_path_unification.py`
(5 testes), usando um novo worker real
(`tests/_helpers/crash_fresh_gate_worker.py`, invocado via
`subprocess.run()`):
- **Cenário exato — variante APPROVE**: `runner` conclui 1x → gate
  encerra o processo via `os._exit()` → lease confirmado com
  `status='GATE_IN_PROGRESS'` E `outcome_json` PERSISTIDO (nunca
  `IN_PROGRESS` sem outcome, o bug original) → aguarda TTL real →
  nova instância faz reclaim → `runner` permanece 0 chamadas NESTA
  instância (o efeito já ocorreu no subprocesso — nunca reexecutado)
  → gate saudável executa 1x → `VERIFIED`.
- **Variante REJECT**: mesmo cenário, gate saudável REJEITA — sem
  retries, `FAILED` definitivo, nunca `VERIFIED`.
- **REJECT com retries disponíveis**: gera uma tentativa NOVA e
  genuína de `task_runner` (nunca reaproveita o efeito original do
  subprocesso morto).
- Controle negativo: nunca reclama antes do TTL expirar.
- Confirmado que tarefas sem `quality_gate` continuam funcionando
  normalmente (nenhuma regressão no caso comum).

## Preservação confirmada explicitamente

Reexecutei os 26 arquivos de teste dedicados de TODAS as rodadas
anteriores — **300 testes, todos passando sem alteração**, incluindo
os 85 testes das oito rodadas anteriores de ATLAS EXPANSION 1 (sem
NENHUMA modificação necessária neles) — toda a proteção
multi-instância, TTL, fencing generation e heartbeat permanece
intacta e agora também protege o caminho fresco.

## Arquivos alterados
- `atlas/orchestration/graph_executor.py` — novo
  `_persist_outcome_and_claim_gate()`; `_run_exclusive_retry_loop()`
  reestruturado — o bloco `run_status == "OK"` agora persiste o
  outcome e delega para `_finalize_pending_gate_outcome()` quando há
  `quality_gate`, removendo toda a lógica antiga inline (mais
  frágil, sem heartbeat/TTL/reclaim aplicáveis)

## Arquivos novos
- `tests/_helpers/crash_fresh_gate_worker.py` — worker standalone que
  conclui o `task_runner` dentro do timeout e morre via `os._exit()`
  dentro do checker, usado pelos testes via subprocesso real
- `tests/test_microcorrecao_atlas_expansion1_fresh_path_unification.py` (5 testes)

## Pendências reais (honestamente declaradas)
- `pytest` continua indisponível neste ambiente
- A mesma pendência das rodadas anteriores permanece válida: um
  outcome preso em `GATE_IN_PROGRESS` porque o processo morreu ANTES
  de qualquer heartbeat renovar (janela extremamente estreita, mas
  teoricamente possível) só se recupera após o TTL — nenhuma forma
  automática de detecção instantânea de crash foi implementada
  (deliberadamente, fora do escopo desta correção)
