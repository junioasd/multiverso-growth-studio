# CHANGELOG — Microcorreção ATLAS EXPANSION 1: Recuperação de Crash Real em GATE_IN_PROGRESS (TTL + Fencing Generation)

Base: `MULTIVERSO_GROWTH_STUDIO_MASTER_V1_EXPANSION_V6_1_1_ATLAS_EXPANSION1_ORPHANED_LEASE_FIX`
(1685 testes). Corrige a falha real reproduzida pela auditoria com um
subprocesso real, preservando integralmente V1 + demais correções da
Expansion 1.

## Resultado real — 3 execuções consecutivas, verificação máxima

```
COLLECTED: 1691  PASS: 1691  FAIL: 0  ERROR: 0  SKIP: 0
DURATION: ~101-103s (3 execuções consecutivas, -W error::ResourceWarning)
EXIT CODE: 0 (as 3 vezes)
ResourceWarnings: 0, threads residuais: 0, subprocessos órfãos: 0
compileall: OK
```

**1691 testes** (1685 preservados + **6 testes novos**).

## Falha real confirmada (reproduzida com subprocesso real)

A microcorreção anterior protegia contra uma EXCEÇÃO TRATÁVEL do
`quality_gate_checker` — mas se o PROCESSO que reivindicou
`GATE_IN_PROGRESS` morresse ABRUPTAMENTE (crash real, `os._exit()`,
queda de máquina — nunca uma exceção Python capturável), o lease
ficava preso nesse estado PARA SEMPRE. Reproduzido com um
`subprocess.run()` real: `TIMED_OUT_UNKNOWN_OUTCOME` → outcome tardio
→ subprocesso reivindica o gate → `os._exit(1)` dentro do próprio
checker → lease confirmado preso em `GATE_IN_PROGRESS` → instâncias
saudáveis subsequentes retornavam `TIMED_OUT_UNKNOWN_OUTCOME`
indefinidamente, o Quality Gate nunca era avaliado, o outcome já
obtido de verdade pelo `task_runner` nunca era finalizado.

## Correção — TTL + reclaim atômico + fencing generation

### `leased_at` participa da detecção de expiração

Novo parâmetro `GraphExecutor(..., gate_in_progress_ttl_seconds=60.0)`
(padrão de produção; testes usam um TTL curto para viabilidade
prática). Ao encontrar um lease em `GATE_IN_PROGRESS`, calcula
`time.time() - leased_at` — dentro do TTL, nunca reclama (alguém pode
genuinamente estar avaliando o gate agora); expirado, é elegível para
reclaim.

### Reclaim atômico via compare-and-set

`UPDATE ... SET lease_token = ?, leased_at = ?, generation = generation + 1
WHERE status = 'GATE_IN_PROGRESS' AND leased_at = ?` — condicionado
não só ao `status`, mas ao `leased_at` EXATO que acabamos de ler.
Se outra instância já reclamou (ou o detentor original, afinal,
resolveu) entre a leitura e este `UPDATE`, `leased_at` já mudou e
`rowcount` é 0 — nunca duas instâncias reclamam o mesmo lease
expirado ao mesmo tempo. Laço limitado (nunca recursão irrestrita) —
até 10 tentativas de releitura em caso de corrida, suficiente para
qualquer cenário realista.

### Fencing token + generation

Nova coluna `generation INTEGER NOT NULL DEFAULT 0` (migração
aditiva — testada com um banco legado sem a coluna) — contador
monotônico, incrementado a cada reivindicação/reclaim, nunca
decresce. Complementa `lease_token` (já rotacionado a cada
reivindicação — já era, por construção, um fencing token UUID) com
um valor totalmente ordenado, útil para diagnóstico (quantas vezes
esta tarefa precisou ser reivindicada de novo). Qualquer instância
com o token antigo (o processo morto) nunca mais consegue resolver o
lease — `_resolve_lease()` já era (e continua sendo) condicionado ao
`lease_token`, que a partir do reclaim nunca mais bate para o token
antigo.

Mesma disciplina aplicada ao caminho em memória, para consistência
(um `GATE_IN_PROGRESS` que nunca resolve, mesmo sem o processo
inteiro ter morrido, também se torna elegível para reclaim após o
TTL).

## Validado com os cenários exatos exigidos

`tests/test_microcorrecao_atlas_expansion1_crash_recovery.py` (6
testes), usando um worker real (`tests/_helpers/crash_gate_worker.py`)
invocado via `subprocess.run()`:
- **Cenário exato**: timeout → outcome tardio → subprocesso real
  reivindica o gate e morre com `os._exit()` → lease confirmado preso
  em `GATE_IN_PROGRESS` → aguarda expiração real do TTL → nova
  instância faz reclaim → `runner` permanece EXATAMENTE 1x, gate
  executa EXATAMENTE 1x, finaliza `VERIFIED`.
- Controle negativo: reclamar ANTES do TTL expirar nunca reclama —
  devolve `TIMED_OUT_UNKNOWN_OUTCOME` imediatamente, nunca gira.
- `generation` confirmado avançando a cada reclaim.
- **Corrida de 3 instâncias** tentando reclaim simultaneamente após o
  crash real do subprocesso — zero exceções de concorrência, gate
  executado exatamente 1 vez, `runner` nunca reexecutado, estado
  final sempre `VERIFIED` consistente.
- Migração aditiva da coluna `generation` — banco legado sem a
  coluna migra com segurança, nunca perde um lease já persistido.
- Confirmado sem flakiness rodando o cenário completo 5× seguidas.

## Preservação confirmada explicitamente

Reexecutei os 24 arquivos de teste dedicados de TODAS as rodadas
anteriores — **292 testes, todos passando sem alteração**, incluindo
os 76 testes das seis rodadas anteriores de ATLAS EXPANSION 1 (sem
NENHUMA modificação necessária neles).

## Arquivos alterados
- `core/models/db.py` — nova coluna `generation` na tabela
  `graph_executor_task_leases` (schema base + migração aditiva)
- `atlas/orchestration/graph_executor.py` — `gate_in_progress_ttl_seconds`
  no construtor; `_acquire_lease_durable()` reestruturado com
  `_read_and_maybe_reclaim_lease_durable()` (loop limitado, TTL,
  reclaim atômico); `_acquire_lease_memory()` com a mesma disciplina

## Arquivos novos
- `tests/_helpers/crash_gate_worker.py` — worker standalone que
  reivindica o gate e morre via `os._exit()`, usado pelos testes via
  subprocesso real
- `tests/test_microcorrecao_atlas_expansion1_crash_recovery.py` (6 testes)

## Pendências reais (honestamente declaradas)
- `pytest` continua indisponível neste ambiente
- O TTL é uma heurística de tempo — um detentor genuinamente lento
  (mas vivo) para avaliar o gate além do TTL configurado teria seu
  lease reclamado por engano; isso é uma limitação INERENTE de
  qualquer mecanismo de lease baseado em tempo (não específica desta
  implementação) — o valor padrão de produção (60s) foi escolhido
  para ser generoso o suficiente para checkers reais, mas o
  responsável pela integração deve calibrar `gate_in_progress_ttl_seconds`
  para o tempo máximo esperado de avaliação do seu `quality_gate_checker`
