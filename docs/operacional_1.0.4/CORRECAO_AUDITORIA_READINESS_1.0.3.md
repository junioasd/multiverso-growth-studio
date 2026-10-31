# CORREÇÃO DA AUDITORIA DE READINESS 1.0.3 → 1.0.4-OPERATIONAL-CANDIDATE

- **Auditoria:** `AUDITORIA_INDEPENDENTE_READINESS_OPERATIONAL_1_0_3_2026-09-25.md` (nesta pasta), com veredito **NÃO APROVADA** e 4 bloqueadores.
- **Predecessores (inalterados):** 1.0.3 `58eaac3e…db26`, 1.0.2 `15aa6b14…9386`, 1.0.1 `a04d5093…c86c9`, v1.0.0 `932292a9…e42fe`.
- **Status:** CANDIDATE — nova auditoria independente de readiness necessária.

## Resultado
| | 1.0.3 (auditada) | 1.0.4 |
|---|---|---|
| Testes | 2.097 | 2.113 (+16) |
| PASS / FAIL / ERROR / SKIP | 2.095 / 0 / 0 / 2 | 2.111 / 0 / 0 / 2 |
| B1 kill switch não configurado, APP_ENV=production | **I/O executado** | bloqueado (`KILL_SWITCH_NOT_CONFIGURED`) |
| B2 trava ativada entre checagem e transporte | **requisição saiu** | 0 requisições (checagem no transporte) |
| B3 reserva USD, custo real EUR | `COST_UNKNOWN`, **sem incidente**, próxima autorizada | `CURRENCY_MISMATCH_INCIDENT`; provedor bloqueado |
| B4 busca com CostGuard, sem limite | **executou; ledger vazio** | sem classificação: `PAYMENT_CLASSIFICATION_REQUIRED`; paga sem limite: `PAID_PROVIDER_NOT_AUTHORIZED` |

**Evidência:** `reproducao_bloqueadores_readiness_1.0.3.py`, com saídas `reproducao_ANTES_1.0.3.txt` e `reproducao_DEPOIS_1.0.4.txt`.

**Nota de transparência:** a 1ª versão do script tornava a linha B2 enganosa na 1.0.4. A busca sem classificação era barrada pelo B4 **antes** de montar a requisição, e a trava nunca chegava a ser ativada. O cenário B2 passou a usar um provedor classificado como gratuito, de modo que o transporte é de fato exercitado (estado `ENABLED`, 0 requisições).

## Causas-raiz e correções
1. **B1:** `kill_switch.state()` tratava "sem configuração" como permitido, sem distinguir produção ou processo operacional.
   - Em `APP_ENV=production` **ou** em processo operacional, a falta de configuração dá `KILL_SWITCH_NOT_CONFIGURED` (bloqueio). Os processos `main.py serve/worker/scheduler/backup/killswitch/readiness` se declaram operacionais via `set_operational_runtime(True)`.
   - O modo permissivo passou a ser **explícito** (`NOT_CONFIGURED_LIBRARY_MODE`): apenas uso como biblioteca/testes, fora de produção.
   - Banco inacessível, linha ausente, estado inválido e erro de leitura também bloqueiam (testes 2–5).
2. **B2:** a guarda rodava antes de montar a requisição, e não no envio.
   - **Transporte central obrigatório** `core/operations/egress.py::urlopen`: a última checagem acontece **no ponto de envio**, depois de toda a preparação.
   - As 6 chamadas `urlopen` do código de produção passam por ele: busca, página, OAuth2 (troca e renovação), webhook (a cada tentativa), adapter de plataforma e geração.
   - Navegador: checagem imediatamente antes de cada `page.goto`.
   - **Proteção estrutural (teste 14):** `urllib.request.urlopen` direto só existe no transporte central.
   - **Limite físico:** entre a última checagem e o envio do socket resta uma janela de instruções, sem nenhuma preparação no meio. Nenhum sistema elimina isso atomicamente através da rede.
3. **B3:** a divergência de moeda caía no ramo "custo desconhecido".
   - Custo real em moeda diferente da reservada, ou sem moeda, abre `CURRENCY_MISMATCH_INCIDENT`: a reserva é cobrada, o custo real **não é convertido** e o provedor fica bloqueado até revisão do Owner.
   - A moeda passou a exigir o formato ISO 4217 (3 letras maiúsculas) em reserva, limite e registro de uso.
4. **B4:** a busca não tinha política de custo.
   - Caminho único `cost_guard.run_metered_call`: classificação → (pago) reserva atômica → I/O → liquidação pelo desfecho real. É usado pela busca e pela geração.
   - A busca exige `paid=True/False` (ou `MGS_SEARCH_PROVIDER_PAID`): sem classificação, `PAYMENT_CLASSIFICATION_REQUIRED`; gratuita, a chamada é auditada (`FREE`, cobrança 0).
   - `PROVIDER_COST_POLICY` classifica **todo** módulo com I/O externo; o teste 14 exige classificação para qualquer módulo novo.
   - Em produção sem CostGuard configurado: `COST_GUARD_NOT_CONFIGURED`.

**Endurecimento adicional:** valores acima de 10¹² são inválidos. Falha da guarda na reserva dá `COST_GUARD_UNAVAILABLE`, e **nenhum I/O** acontece. Falha na liquidação mantém a reserva contando e abre `SETTLEMENT_FAILED`, o que bloqueia o provedor. Crash, timeout e reservas abandonadas seguem cobertos pela 1.0.2.

## Arquivos
- **Produção:**
  - `core/operations/kill_switch.py`
  - `core/operations/egress.py`
  - `core/operations/cost_guard.py`
  - `core/agents/runtime/http_search_provider.py`
  - `core/agents/runtime/http_generation_provider.py`
  - `core/multimodal/real_webpage_analyzer.py`
  - `bridge/execution/oauth2.py`
  - `bridge/execution/webhooks.py`
  - `bridge/execution/platform_adapters.py`
  - `bridge/execution/browser_executor.py`
  - `main.py`
- **Testes:** novo `tests/test_operacional_1_0_4.py` (16 testes, cobrindo os itens 1–28).
- **Testes existentes ajustados** (2, consequência direta e exigida das correções):
  - `tests/test_operacional_1_0_3.py::EgressInventoryTests`: o inventário **exato** de I/O mudou por desenho (transporte central novo e I/O da busca movido para `_perform`). O teste existe para forçar essa revisão consciente, e a regra "guarda antes do I/O" continua valendo.
  - `tests/test_operacional_1_0_3.py::test_guard_releases_when_disabled`: a busca precisa de classificação de custo (B4). Foi declarada `paid=False`, preservando a intenção (a trava libera quando desligada).

## Testes e mutação
- **Suítes:** 1.0.4 16/16; 1.0.3, 1.0.2, 1.0.1, correções de auditoria e replay legado verdes. Runner estrito RC 0.
- **Regressão completa:** 2.113 testes / 2.111 PASS / 0 FAIL / 0 ERROR / 2 SKIP (Windows real).
- **Mutação: 11 de 11 mutantes válidos detectados:**
  - B1 original; produção ignorada;
  - transporte sem checagem; navegador sem checagem antes do `goto`;
  - moeda divergente sem incidente; moeda inválida aceita;
  - busca sem governança; sem classificação presumido gratuito;
  - falha de liquidação sem incidente; guarda indisponível liberando I/O;
  - valores extremos aceitos.
- **Transparência:** dois mutantes da 1ª rodada eram **inválidos** (alvo com barra a mais; erro de sintaxe em `pass or`). Foram refeitos e detectados pelos testes esperados.

## Pendências
- **Internas restantes:** nenhuma identificada nesta candidata.
- **Exclusivas do Owner:**
  - `MGS_VAULT_MASTER_KEY`, `SESSION_SECRET` e credencial do Owner;
  - volume de produção e proxy TLS;
  - destino externo de backup;
  - limites de custo por provedor;
  - **classificação de custo da busca** (`MGS_SEARCH_PROVIDER_PAID`);
  - supervisor de processos.
- **Exclusivas de serviços externos:** as 12 integrações do Day 0 (nenhuma conectada).
