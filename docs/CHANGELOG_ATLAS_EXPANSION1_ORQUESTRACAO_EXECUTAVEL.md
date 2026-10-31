# CHANGELOG — ATLAS EXPANSION 1: Orquestração Executável

Base: `MULTIVERSO_GROWTH_STUDIO_MASTER_V1_EXPANSION_V6_1_1_SHUTDOWN_RESIDUAL`
(1609 testes). Primeira entrega da expansão do ATLAS — fundação de
execução real de grafos/loops/paralelismo, preservando integralmente
a V1 aprovada.

## Resultado real — 3 execuções consecutivas, verificação máxima

```
COLLECTED: 1643  PASS: 1643  FAIL: 0  ERROR: 0  SKIP: 0
DURATION: ~88-90s (3 execuções consecutivas, -W error::ResourceWarning)
EXIT CODE: 0 (as 3 vezes)
ResourceWarnings: 0, threads residuais: 0
```

**1643 testes** (1609 preservados + **34 testes novos**: 31 do
executor + 3 de integração com `AtlasDirector`).

## O que foi construído

Executor REAL de `AdvancedPlan` — sobre a infraestrutura já existente
e aprovada, **sem duplicar nenhum módulo**:

- `atlas.advanced_planning.AdvancedPlan.parallel_groups()` (já
  existente) — ordenação topológica, agrupamento por nível de
  paralelismo, rejeição de ciclo. Este executor apenas CONSOME esse
  resultado.
- `core.coordination.mission.MissionTracker` (já existente) — state
  machine completa com transições condicionadas a versão. Este
  executor usa essa state machine para rastrear cada tarefa.
- `core.jobs.queue.JobQueue` (já existente) — enqueue idempotente,
  `start()` atômico com capacidade real, retry via
  `attempts`/`max_attempts`. Este executor delega cada tarefa a um
  `Job`.
- `atlas.escalation.EscalationDesk` (já existente) — Owner Approval
  fail-closed. Este executor escalona tarefas críticas por ele.

Novo módulo: `atlas/orchestration/graph_executor.py` —
`GraphExecutor` (motor de execução), `LoopSpec`/`run_loop()` (loops
com condição de parada explícita + limite de iterações + fail-closed).
Conectado a `AtlasDirector` como `self.graph_executor`, compartilhando
as MESMAS instâncias reais de `missions`/`facade.job_queue`/
`escalation_desk`.

## 3 bugs reais encontrados e corrigidos durante o desenvolvimento

1. **Dependências mal-mapeadas**: `MissionAssignment.dependencies`
   usa semântica de `mission_id` CRUZADO (entre missões diferentes) —
   popular esse campo com `task.depends_on` (que referencia `task_id`
   DENTRO do mesmo plano) travava a missão em `QUEUED` para sempre
   (`DependencyNotSatisfied` incorreta). Corrigido: `dependencies=[]`
   nas missões deste executor — a dependência intra-plano já é
   estruturalmente garantida por `parallel_groups()`.
2. **Transição de estado faltando**: a state machine exige
   `QUEUED → PLANNED → RUNNING` (nunca direto) — `start_running()`
   falhava porque a missão recém-criada nunca passava por `PLANNED`.
   Corrigido com a transição explícita antes de `start_running()`.
3. **Timeout anulado pelo próprio `ThreadPoolExecutor`** (o mais
   sério): `with ThreadPoolExecutor(...) as single:` faz `__exit__`
   chamar `shutdown(wait=True)` IMPLICITAMENTE ao sair do bloco —
   mesmo desistindo via `FutureTimeoutError`, a saída do `with`
   ficava bloqueada esperando a thread terminar sozinha. Medido: um
   `timeout_per_task=0.2s` contra uma tarefa de 10s ainda levava os
   10s inteiros. Pior: mesmo trocando para `shutdown(wait=False)`
   explícito, `concurrent.futures` registra um `atexit`
   (`_python_exit`) que junta TODA thread já criada por qualquer
   `ThreadPoolExecutor` do processo — o PROCESSO (não só a
   orquestração) ficava preso por até a duração real da tarefa lenta.
   Corrigido substituindo por uma `threading.Thread(daemon=True)`
   crua para o timeout por tarefa — medido: processo passou a
   encerrar em ~0.28s em vez de ~2.08s. Esta é a mesma classe de
   problema já corrigida em `RadarWorker`/`RadarWebWorker`
   (microcorreção de shutdown residual), agora na raiz do próprio
   `ThreadPoolExecutor`.

## Validado com os cenários exatos exigidos

`tests/test_atlas_expansion1_graph_executor.py` (31 testes):
- **DAG válido**: dependências respeitadas, ordem de execução
  confirmada, missões chegam a `VERIFIED`.
- **Ciclo rejeitado**: direto, indireto, e dependência desconhecida —
  todos na CONSTRUÇÃO do plano, o executor nunca chega a rodar nada
  de um plano inválido.
- **Execução paralela comprovada por concorrência REAL**:
  `threading.Barrier` — se a execução fosse sequencial, o teste
  TRAVARIA até o timeout do Barrier (prova determinística, nunca um
  "passa por sorte" de timing). Complementado por prova de tempo (N
  tarefas de duração D completam em ~D, nunca N×D) e confirmação de
  que grupos DEPENDENTES nunca se sobrepõem.
- **Falha parcial**: grupo com falha bloqueia o grupo seguinte
  (fail-closed), mas tarefas irmãs bem-sucedidas continuam
  `VERIFIED`.
- **Retry real**: via `JobQueue`, respeitando `max_retries` — sucesso
  após N tentativas, falha definitiva após esgotar.
- **Timeout real**: orquestração desiste no tempo configurado
  (confirmado <1s mesmo com tarefa de 1.5s), nunca espera a tarefa
  lenta terminar.
- **Cancelamento cooperativo**: durante execução e antes de começar —
  grupos seguintes nunca iniciam.
- **Shutdown limpo**: 0 threads residuais após execução normal,
  timeout e cancelamento — a thread órfã de um timeout é daemon,
  termina sozinha, nunca bloqueia o processo.
- **Retomada sem duplicidade**: mesmo `mission_id` reexecutado nunca
  repete tarefas já `VERIFIED`; simulação de restart (novo
  `GraphExecutor`, mesmos trackers) só reexecuta o que ficou pendente.

Adicional (infraestrutura mencionada no pedido, testada mas fora da
lista obrigatória): Owner Approval real (bloqueia sem aprovação,
prossegue após aprovar) e o construtor `LoopSpec`/`run_loop()`
isoladamente (converge antes do limite, fail-closed ao esgotar,
rejeita `max_iterations<=0`, condição checada antes do corpo).

## Correção adicional necessária — falso positivo em checagem própria

Durante a regressão completa, `test_microcorrecao_seguranca_bloco_final2.
RequirementsCompletenessRegressionTests` (scanner estático de uma
rodada anterior) sinalizou `concurrent` (de `concurrent.futures`,
usado no novo módulo) como dependência de terceiros ausente —
`concurrent` é da biblioteca padrão do Python, só faltava na lista
interna do próprio scanner (mesma classe de falso positivo já
corrigida antes para `socketserver`). Corrigido.

## Escopo — o que NÃO foi implementado nesta entrega

Conforme solicitado explicitamente: nenhuma coleta de redes sociais e
nenhuma produção de vídeo. `task_runner`/`quality_gate_checker`/
`requires_owner_approval` são sempre fornecidos pelo CHAMADOR — este
módulo é inteiramente agnóstico de domínio, pronto para os blocos
seguintes (os 3 pipelines video-first/multiformato) se conectarem
sem exigir nenhuma mudança na orquestração em si.

## Arquivos novos
- `atlas/orchestration/__init__.py`
- `atlas/orchestration/graph_executor.py`
- `tests/test_atlas_expansion1_graph_executor.py` (31 testes)
- `tests/test_atlas_expansion1_director_wiring.py` (3 testes)

## Arquivos alterados
- `atlas/director.py` — `self.graph_executor` conectado
- `tests/test_microcorrecao_seguranca_bloco_final2.py` — `concurrent`
  adicionado à lista de módulos stdlib conhecidos (falso positivo)

**Nenhuma funcionalidade já aprovada foi removida ou alterada** — 210
testes dedicados de todas as rodadas anteriores (BF1/BF2/BF3 +
composite-key + shutdown residual) reexecutados explicitamente, sem
alteração.

## Pendências reais (honestamente declaradas)
- `pytest` continua indisponível neste ambiente
- Owner Approval neste executor segue o padrão estabelecido (cria
  escalação, bloqueia, exige nova chamada com `approved_escalation_ids`
  após aprovação) — nunca bloqueia esperando internamente, consistente
  com `NavigatorReal`, mas significa que o CHAMADOR é responsável por
  reinvocar `execute_plan()` após a aprovação real acontecer
