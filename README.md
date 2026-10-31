# MULTIVERSO GROWTH STUDIO — Master V1 + ATLAS + RADAR + NAVIGATOR + FORGE

Projeto separado do MULTIVERSO DIGITAL IA CORE. Owner: Júnior.

## Build state

**MICROCORREÇÃO DE FECHAMENTO ATLAS PIPELINE 1 — DOMAIN POLICY + TEST/REAL REVALIDATION + DURABLE ACQUISITION ANCHOR — IMPLEMENTED + TESTED**

Corrigidas as 3 falhas estruturais finais reportadas por auditoria
independente. (1) Lineage válida aceitava valores fora do domínio
semântico da métrica — `trend_strength=50.0`, `audience=-100`,
`competition=-4.0` com lineage íntegra e `trust_issues()` vazio ainda
chegavam a `READY_FOR_PIPELINE2`. Corrigido com `_SIGNAL_DOMAINS`
centralizado (`trend_strength`/`competition`/`risk`/`hook_strength`/
`compliance` em `[0,1]`; `audience`/`estimated_reach`/
`production_cost` em `[0,∞)`) — um valor fora do domínio é REJEITADO,
nunca corrigido via clamp silencioso. (2) Lineage `TEST` sobrevivia a
restart e virava utilizável em modo operacional — persistida com
`allow_test_sources=True`, uma nova instância com
`allow_test_sources=False` ainda aprovava. Corrigido com um novo
`TrustPolicy`, construído a partir da configuração ATUAL do engine e
reavaliado a CADA chamada de `trust_issues()` — nunca fixado
permanentemente na criação da lineage; o `mode` gravado nunca muda
(origem sempre honesta), só a decisão de aceitá-lo é reavaliada. (3)
`VerifiedLineage` não era ancorada a uma prova durável autoritativa —
trocar `acquisition_id` por uma string inexistente, mantendo o valor
e seu digest intactos, ainda validava. Corrigido com uma tabela SQLite
SEPARADA (`discovery_acquisitions`) — `DurableAcquisitionRecord`, o
registro AUTORITATIVO (nunca segredo/token) que `VerifiedLineage`
referencia e que toda revalidação busca de novo, cruzando
`acquisition_id`/`source_id`/`execution_id`/`mode`/`status`/
`data_status`/`observations_digest`. As três correções convergem para
UMA validação canônica única (`DiscoverySignal.trust_issues()`),
nunca três validadores separados. Validado com os 10+5+11 cenários
adversariais exigidos — incluindo "alterar individualmente cada campo
persistido" com SQLite real. Todos os 456 testes dedicados de todas
as rodadas anteriores continuam passando sem nenhuma modificação de
comportamento. 1855 testes, `compileall` OK, suíte completa executada
3 vezes consecutivas com `-W error::ResourceWarning` — 1855/1855 PASS
nas 3, exit code 0, 0 threads residuais, stderr limpo. Ver
`docs/CHANGELOG_ATLAS_PIPELINE1_DOMAIN_POLICY_DURABLE_ANCHOR.md` para
os detalhes completos.

## Começando

```bash
python3 main.py set-owner-password
python3 main.py health
python3 -m unittest discover -s tests -v
python3 main.py serve
```

## Uso

```python
from api.bootstrap import build_app
app = build_app()

# ATLAS delega a NAVIGATOR (sistema legado de missão/tarefa)
mission = app.atlas.delegate_mission(agent="NAVIGATOR", objective="coletar métricas do canal")
entry = app.atlas.navigator.execute(
    platform="youtube", action=..., payload={}, mission_id=mission.mission_id, task_id=mission.task_id,
)

# FORGE diagnostica
diag = app.atlas.forge.detect(issue_summary="latência alta", evidence={"p99_ms": 900})

# Biblioteca Mestre
app.atlas.consult_library(area="marketing")
```

### NavigatorReal / RadarReal / adapters (Bloco Final 2 — execução externa real)

Separado do `app.atlas.navigator` (missão/tarefa, acima), `app.atlas.
navigator_real` é o motor de execução externa real — aplica a
prioridade oficial (API oficial > conector/MCP > navegador >
MOCK_FALLBACK, nunca disfarçado de sucesso), autoridade de
criticidade que o chamador nunca rebaixa, claim atômico anti-replay, e
bloqueio de ações críticas sem aprovação do Owner:

```python
from bridge.execution.navigator_real import ActionCriticality

# acao rotineira -- sem credencial real configurada, reporta
# honestamente qual mecanismo faltou (nunca finge sucesso)
result = app.atlas.navigator_real.execute_action(
    platform="youtube", action="GET_CHANNEL_INFO", criticality=ActionCriticality.ROUTINE,
    params={"channel_id": "UC_exemplo"}, actor="ARGOS", adapter_method="get_channel_info",
)
print(result.success, result.reason)  # False, "apenas MOCK_FALLBACK disponível..." sem credencial real

### GraphExecutor (ATLAS EXPANSION 1 — orquestração executável)

Executor real de `AdvancedPlan` — despacha tarefas independentes em
paralelo real, bloqueia dependentes até os pré-requisitos concluírem,
com retry/timeout/cancelamento/Owner Approval reais:

```python
from atlas.advanced_planning import AdvancedPlanner

planner = AdvancedPlanner()
plan = planner.build_plan(objective="coletar e processar", tasks=[
    {"task_id": "coleta_a", "description": "coleta A"},
    {"task_id": "coleta_b", "description": "coleta B"},
    {"task_id": "consolida", "description": "consolida", "depends_on": ["coleta_a", "coleta_b"]},
])

def meu_task_runner(task):
    # aqui entra o efeito real de UMA tarefa (agnóstico de domínio —
    # os pipelines de vídeo/coleta de redes se conectam aqui em
    # blocos futuros, nunca dentro do executor em si)
    return {"tarefa": task.task_id, "ok": True}

report = app.atlas.graph_executor.execute_plan(plan, task_runner=meu_task_runner, actor="ARGOS")
print(report.success)  # True -- coleta_a/coleta_b rodaram em paralelo, consolida so depois das duas

# RADAR REAL -- nunca inventa oportunidade; sempre tenta acesso real
# antes de validar (via BrowserExecutor/Playwright)
opportunity = app.atlas.radar_real.discover(candidate_url="https://marketplace-real.example/produto")
print(opportunity.validation_status)  # VALIDATED, SOURCE_UNAVAILABLE, ou INVALID_URL — nunca fabricado
```

Adapters reais registrados em `app.atlas.navigator_real.
adapters_by_platform`: YouTube (Data API v3), Meta/Facebook/Instagram
(Graph API), TikTok, Hotmart, Monetizze, Google Analytics, Google Ads
(somente leitura) — cada um em `bridge/execution/platforms.py` e
`bridge/execution/commercial_platforms.py`, com credenciais lidas do
`TokenVault` (`app.atlas.token_vault`), nunca hardcoded.

## Escopo desta entrega

- `app.atlas.navigator_real`/`app.atlas.radar_real` (Bloco Final 2):
  execução externa REAL — API oficial real, navegador real
  (Playwright), claim atômico, criticidade autoritativa; sem
  credencial configurada, reportam honestamente indisponibilidade,
  nunca simulam sucesso.
- `app.atlas.navigator`/`app.atlas.radar` (sistema legado de missão/
  tarefa, preservado sem alteração): mecanismo de execução por
  plataforma configurável via `register_mechanism_executor` —
  MOCK_FALLBACK por padrão até um executor real ser registrado para
  aquela plataforma/mecanismo.
- Nenhuma credencial real incluída neste pacote.
- Todo o hardening V1-V6, ATLAS V1 e ATLAS+RADAR preservados sem
  alteração de comportamento — apenas extensões aditivas.

## Segurança

Nenhum segredo real está neste pacote. Ver `docs/SECURITY.md` para o
histórico completo de hardening, `docs/ATLAS.md`/`docs/ATLAS_RADAR.md`
para o Diretor Operacional e o Radar, e
`docs/ARCHITECTURE_EXPANSION.md` para a arquitetura multiagente
completa (NAVIGATOR, FORGE, BRIDGE, SENTINEL).
