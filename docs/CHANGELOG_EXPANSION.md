# CHANGELOG — Expansão Oficial do MULTIVERSO GROWTH STUDIO

Base: `MULTIVERSO_GROWTH_STUDIO_MASTER_V1_ATLAS_RADAR_FIX_V3` (preservado
integralmente — nenhum arquivo aprovado foi removido ou enfraquecido).

## Novidades

### NAVIGATOR — Executor Operacional Multicanal (`navigator/`)
- `models.py` — ações operacionais (`access_platform`, `locate_area`,
  `fill_form`, `create_draft`, `publish`, `reply`, `update_content`,
  `collect_metrics`, `check_status`, `send_evidence`); publicar/
  responder/atualizar conteúdo são sempre críticas.
- `adapters.py` — 17 plataformas cobertas nesta entrega (redes sociais,
  sites, blogs, CMS, marketplaces, afiliados, lojas, CRM, páginas de
  venda, landing pages, painéis administrativos, anúncios, comunidades,
  fóruns, diretórios), todas em MOCK (sem integração externa real).
- `execution_log.py` — trilha persistida com todos os campos exigidos.
- `executor.py` — `NavigatorExecutor`, exige `Principal(role=NAVIGATOR)`;
  toda ação crítica SEMPRE escalonada ao Owner via o MESMO
  `EscalationDesk` do ATLAS; isolamento via `IsolationGuard` já
  existente; idempotência por (mission_id, task_id, action); retry com
  limite; executores temporários especializados.

### FORGE — Engenharia, Manutenção e Reparo (`forge/`)
- `models.py` — pipeline de 10 estados ordenados (DETECTED→RELEASED),
  mais BLOCKED/ROLLED_BACK.
- `engineer.py` — `ForgeEngineer`, exige `Principal(role=FORGE)`; nunca
  pula etapa do pipeline; `apply_fix()` exige autorização explícita
  (Owner ou ATLAS — risco ALTO exige o próprio Owner); bloqueio
  preventivo de operação (`block_operation`).

### Camada comum de raciocínio (`core/reasoning/`)
- `cycle.py` — `ReasoningTrace` com o ciclo OBSERVAR→...→REPORTAR;
  `mark_verified()` recusa estruturalmente evidência vazia.
- `self_correction.py` — classificação de erro, limite de tentativas
  explícito, escalonamento obrigatório (nunca loop infinito).

### Planejador avançado (`atlas/advanced_planning.py`)
- Dependências, grupos paralelos, prioridade, prazo, recursos,
  ferramentas, riscos, critérios de conclusão, fallback, retry,
  rollback, checkpoints, Quality Gate; detecção de ciclo; `replan()`.

### Coordenação multiagente (`core/coordination/mission.py`)
- `MissionTracker` persistido — 11 estados exigidos
  (QUEUED→...→VERIFIED/CANCELLED); UPDATE condicionado a versão (nunca
  reverte estado terminal).

### Memória de trabalho e Biblioteca Mestre
- `core/memory/working_memory.py` — curto prazo / contexto de tarefa /
  operacional / histórico de decisões / aprendizados (só reutilizáveis
  após validação explícita); segredos sempre redigidos.
- `core/library/master_library.py` — conhecimento versionado; revisão
  nunca sobrescreve validado sem `propose_revision`+`approve_revision`.

### BRIDGE (`bridge/hub.py`)
- Só conecta: credenciais como referências (nunca segredo real),
  registro de conectores, ordem de mecanismo API→MCP→webhook→
  navegador→RPA.

### SENTINEL (`atlas/sentinel.py`)
- Envolve o `QualitySupervisor` já existente com vocabulário
  PASS/FAIL/BLOCK/WARNING; pode recomendar bloqueio de publicação,
  entrega, campanha, integração, deploy ou operação.

### RBAC estendido
- Papéis `NAVIGATOR` e `FORGE` (menor privilégio, aditivo).
- `CRITICAL_ACTION_TYPES` ampliado com 7 categorias novas (transferência
  de fundos, exclusão destrutiva, alteração de segurança/propriedade,
  mudança estrutural crítica, publicação sensível, ação irreversível) —
  as 5 originais preservadas.

### `AtlasDirector` — MASTER orquestrador
- Novos: `navigator`, `forge`, `sentinel`, `bridge`, `memory`,
  `missions`, `library`, `advanced_planner`, `delegate_mission()`,
  `update_mission_status()`, `consult_library()`,
  `start_reasoning_trace()`.
- Tudo que já existia (radar, scheduler, worker, aprovações
  persistidas, análise multidimensional, etc.) preservado sem alteração
  de comportamento.

### Schema (aditivo)
Novas tabelas: `missions`, `navigator_execution_log`,
`forge_diagnostics`, `master_library_entries`. Nenhuma tabela existente
alterada de forma destrutiva.

## Testes
**CORREÇÃO (item 7 desta auditoria): número anterior incorreto.** A
base real V3 tinha 613 testes; esta expansão somou **118 testes
novos** (613 + 118 = 731) — não "383 novos" como uma versão anterior
deste changelog declarava incorretamente. Cobertura: NAVIGATOR, FORGE,
raciocínio, planejamento avançado, multiagente, memória/biblioteca e
segurança/governança — ver `docs/TEST_REPORT_EXPANSION.md` (contagem
sempre extraída da execução real da suíte, nunca estimada).

## Limitações declaradas
- Nenhuma integração externa real (instrução explícita) — todos os
  adaptadores de plataforma operam em MOCK.
- NAVIGATOR cobre 17 plataformas nesta entrega; arquitetura extensível
  sem alterar `NavigatorExecutor`.
- Comportamento "AGI-like" é estrutural/disciplinar (ciclo de
  raciocínio, autocorreção limitada, evidência obrigatória) — não há
  alegação de AGI real.
