# CORREÇÃO DA AUDITORIA DE READINESS 1.0.2 → 1.0.3-OPERATIONAL-CANDIDATE

- **Auditoria:** `AUDITORIA_INDEPENDENTE_READINESS_OPERATIONAL_1_0_2_2026-09-24.md` (nesta pasta), com veredito **NÃO APROVADA** e 2 bloqueadores críticos.
- **Predecessores (inalterados):** 1.0.2 `15aa6b14…9386`, 1.0.1 `a04d5093…c86c9`, v1.0.0 `932292a9…e42fe`.
- **Status:** CANDIDATE — nova auditoria independente de readiness necessária.

## Resultado
| | 1.0.2 (auditada) | 1.0.3 |
|---|---|---|
| Testes | 2.086 | 2.097 (+11) |
| PASS / FAIL / ERROR / SKIP | 2.084 / 0 / 0 / 2 | 2.095 / 0 / 0 / 2 |
| Kill switch ENABLED: busca, página, OAuth2, navegador | **fizeram I/O** (busca, página) / navegador sem guarda | **0 requisições**; todos bloqueados |
| Estimativa −5 / −100 | **AUTHORIZED** | `INVALID_COST` |
| Estimativa NaN | **IntegrityError** | `INVALID_COST` (negação estruturada) |
| Provedor reporta custo −20 | **saldo aumentou; chamada de 20 passou** | cobra a reserva; incidente; próxima chamada bloqueada |
| Custo real 20 sobre reserva 1 | teto ultrapassado **sem incidente** | incidente `COST_OVERRUN`; provedor bloqueado até revisão |

- **Evidência:** `reproducao_bloqueadores_readiness_1.0.2.py`, com saídas `reproducao_ANTES_1.0.2.txt` e `reproducao_DEPOIS_1.0.3.txt` (mesmo script nos dois pacotes).
- **Nota de transparência:** a linha do OAuth2 nesse script usa configuração montada por adivinhação e não alcança o servidor em nenhum dos dois pacotes. A prova do OAuth2 (troca **e** renovação, com configuração real) é `test_no_known_external_executor_does_io_with_kill_switch_enabled`.

## Bloqueador 1 — kill switch global em todo I/O externo
- **Causa-raiz:** a guarda estava espalhada em caminhos específicos, sem inventário de toda a saída externa.
- **Correção:**
  - guarda central `core/operations/egress.py::egress_blocked()`, chamada imediatamente antes de **cada** I/O externo do código de produção.
  - **inventário completo** (AST) — 9 chamadas de I/O:
    - antes sem guarda: `HttpSearchProvider.call`, `RealWebPageAnalyzer.analyze`, `OAuth2Flow._post_token_request` (troca e renovação), `BrowserExecutor.navigate_and_extract` (caminho do `RadarReal`) e `BrowserExecutor.execute_authenticated_action`;
    - unificados na guarda central: `PlatformApiAdapter._request`, `WebhookDispatcher.deliver` (a cada tentativa) e `HttpGenerationProvider._perform`;
    - exceção justificada: `BrowserExecutor.is_browser_available`, uma sonda **local** de capacidade, sem URL.
- **Proteção contra caminhos novos:** `EgressInventoryTests` exige o inventário **exato** e a guarda antes de cada I/O. Um mutante que acrescentou um `urlopen` sem guarda num módulo qualquer foi detectado.
- **Correção de relato:** eu havia dito "10 pontos" ao contar linhas do grep; o `.launch()` do Chromium fica dentro dos mesmos blocos. O número correto é 9.

## Bloqueador 2 — integridade monetária e política de overrun
- **Causa-raiz:** não havia validação central de domínio para valores monetários.
- **Correção:**
  - `valid_money()`: número real **finito e ≥ 0**; bool, str, NaN, ±Inf e negativos são inválidos.
  - Aplicada em:
    - `reserve()/authorize()` → `INVALID_COST` / `INVALID_CURRENCY`, negação estruturada, nada persistido;
    - `set_limit()`, `record_usage()` (inclusive tokens e unidades) e `reconcile()` → `ValueError`;
    - `settle()` → custo inválido **reportado pelo provedor** nunca abate o gasto: cobra a reserva, marca `COST_INVALID_REPORTED` e abre incidente.
  - **Defesa em profundidade:** *triggers* SQLite em `ai_api_reservations`, `ai_api_usage` e `ai_api_limits` abortam valor negativo, NULL (NaN) ou infinito.
  - **Política de overrun (decisão explícita):** o limite é aplicado **sobre a reserva** — a estimativa informada antes da chamada.
    - Se o custo real vier acima do reservado, ou vier inválido: o gasto real é registrado (nunca escondido), abre-se incidente (`COST_OVERRUN` / `INVALID_REPORTED_COST`) e o provedor fica **bloqueado** (`COST_INCIDENT_REVIEW_REQUIRED`) até o Owner resolver com evidência (`resolve_incident`, exclusivo do Owner).
    - **Isto NÃO é apresentado como teto rígido do gasto real:** um teto rígido real exige limite configurado no próprio provedor.

## Arquivos alterados
- **Produção:**
  - novo `core/operations/egress.py`
  - `core/agents/runtime/http_search_provider.py`
  - `core/multimodal/real_webpage_analyzer.py`
  - `bridge/execution/oauth2.py`
  - `bridge/execution/browser_executor.py`
  - `bridge/execution/webhooks.py`
  - `bridge/execution/platform_adapters.py`
  - `core/agents/runtime/http_generation_provider.py`
  - `core/operations/cost_guard.py`
  - `core/models/db.py` (tabela `ai_api_cost_incidents` e *triggers*, ambos aditivos)
- **Testes:** novo `tests/test_operacional_1_0_3.py` (11 testes). **Nenhum teste existente foi alterado.**

## Testes e mutação
- **Suítes:** 1.0.3 (11/11), 1.0.2 (13/13) e 1.0.1 (24/24) verdes; runner estrito RC 0.
- **Regressão completa:** 2.097 testes / 2.095 PASS / 0 FAIL / 0 ERROR / 2 SKIP (Windows real).
- **Mutação: 12 de 12 detectados**, incluindo os defeitos originais:
  - guarda removida em busca, página, OAuth2 e navegador;
  - guarda central sempre liberando;
  - I/O novo sem guarda;
  - estimativa negativa aceita;
  - validação sem "≥ 0";
  - `settle` ignorando custo inválido;
  - overrun sem incidente;
  - incidente sem bloqueio;
  - *triggers* ausentes.

## Defeitos meus corrigidos durante o trabalho
- A guarda do navegador foi inserida fora do lugar (docstring de linha única); refeita via AST.
- Nome de campo errado no OAuth2 (`token_url` → `token_endpoint`).
- Contagem de pontos de I/O relatada incorretamente (10 → 9).
- Rótulo fixo enganoso no script de reprodução, removido antes de gerar a evidência.
