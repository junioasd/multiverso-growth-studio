# CHANGELOG — Microcorreção BF2: Skip Explícito + Verificação de Hang + README Alinhado

Base: `MULTIVERSO_GROWTH_STUDIO_MASTER_V1_EXPANSION_V6_1_1_BF2_COMPLETE_REDACTION`
(1567 testes). Corrige APENAS os 4 itens solicitados, sem expandir
escopo — preservando integralmente tudo já aprovado.

## Resultado real — 3 execuções consecutivas, Python 3.12 + Chromium, verificação máxima

```
Python: 3.12.3
Chromium: disponível e verificado (is_browser_available() == True)
COLLECTED: 1567  PASS: 1567  FAIL: 0  ERROR: 0  SKIP: 0
DURATION: ~77-81s (3 execuções consecutivas, -W error::ResourceWarning)
EXIT CODE: 0 (as 3 vezes)
Arquivos órfãos: 0
```

## Item 1 — SKIP/UNAVAILABLE explícito sem Chromium/Playwright ✅

**Falha confirmada**: `tests/test_browser_executor.py` (2 testes) e
`tests/test_radar_real.py` (1 teste) exercitavam o navegador REAL sem
checar `is_browser_available()` primeiro. Simulei a ausência do
Chromium (`patch.object(BrowserExecutor, "is_browser_available",
return_value=(False, ...))`) e confirmei: `test_navigates_and_extracts_real_title`
produzia um **FALSE FAIL** genuíno (`AssertionError: False is not true`),
e os outros dois "passavam" pelo motivo ERRADO (a asserção
`success=False` sendo satisfeita tanto por uma falha de DNS real
quanto por um Chromium ausente — mascarando que a integração real não
foi exercitada).

**Corrigido**: os 3 testes agora checam `is_browser_available()`
explicitamente e usam `self.skipTest(...)` quando o binário não está
presente — nunca mais um FALSE FAIL, e nunca mais um PASS enganoso
pelo motivo errado. Revalidei simulando ausência (3 SKIPPED, 0
FAILURES, 0 ERRORS) e com o Chromium real presente (todos executam
normalmente, sem skip).

## Item 2 — Hang dependente da ordem da suíte ✅ (nenhuma correção de código necessária — investigação extensiva confirmou robustez já existente)

Investiguei ativamente com: suíte completa em ordem REVERSA, e 3
sementes de embaralhamento aleatório diferentes (1567 testes cada,
Python 3.12, Chromium real) — **todas as 4 execuções**: 1567/1567
PASS, 0 FAILURES, 0 ERRORS, durações consistentes (~78-81s, sem
anomalia indicando travamento). Uma execução adicional combinou
embaralhamento + `-W error::ResourceWarning` + verificação de threads
residuais via `core.diagnostics.dump_resources()`: 0 ResourceWarnings,
0 threads residuais, `clean=True`.

Durante a investigação, encontrei um FALSO ALARME (não um bug real):
ao invocar um script de teste externo localizado fora do diretório do
projeto (`/tmp`), `sys.path[0]` do Python passa a ser o diretório do
script (não o diretório de trabalho), quebrando a resolução de
`unittest.TestLoader().loadTestsFromName("tests....")` usada
internamente por `tests/test_correction_suite_order_stability.py`.
Isso é um artefato da localização do MEU script auxiliar, não um
problema no código do projeto — confirmado reexecutando o mesmo
teste de embaralhamento a partir de dentro do diretório do projeto,
onde passou limpo em todas as 4 tentativas.

**Conclusão honesta**: a suíte já tinha shutdown limpo de workers/
threads/servidor HTTP real (usado nos testes de anti-replay atômico)
antes desta rodada — as correções de concorrência das rodadas
anteriores do BLOCO FINAL 2 (claim atômico via SQLite, locks
corretos, `tearDown()` sempre chamando `server.shutdown()`) já
garantiam isso. Nenhuma alteração de código foi necessária ou feita
para este item — apenas verificação extensiva e documentada.

## Item 3 — README alinhado ao código real de NavigatorReal/RadarReal/adapters ✅

**Falhas confirmadas**:
1. A seção "Escopo desta entrega" afirmava categoricamente "Nenhuma
   integração externa real (NAVIGATOR/RADAR permanecem em MOCK...)" —
   **falso** desde as rodadas anteriores do BLOCO FINAL 2:
   `app.atlas.navigator_real`/`app.atlas.radar_real` são motores de
   execução REAIS (API oficial real, navegador real via Playwright,
   claim atômico anti-replay, criticidade autoritativa).
2. A seção "Uso" não mencionava `navigator_real`/`radar_real`/
   adapters em nenhum lugar — apenas o sistema legado de missão/
   tarefa (`app.atlas.navigator`, que continua existindo e funcionando
   como documentado, preservado sem alteração).

**Corrigido**:
- Nova subseção "NavigatorReal / RadarReal / adapters" com um
  exemplo de uso **testado e confirmado funcionando de ponta a
  ponta** neste ambiente antes de ser documentado (`app.atlas.
  navigator_real.execute_action(...)` e `app.atlas.radar_real.
  discover(...)`, com as assinaturas reais verificadas diretamente no
  código-fonte).
- "Escopo desta entrega" reescrito para distinguir corretamente os
  DOIS sistemas paralelos que coexistem no `AtlasDirector`:
  `navigator_real`/`radar_real` (real, Bloco Final 2) e `navigator`/
  `radar` (legado, missão/tarefa, MOCK_FALLBACK por padrão até um
  executor real ser registrado — preservado sem alteração).
- Lista de adapters reais registrados (`YouTubeAdapter`,
  `MetaGraphAdapter`, `TikTokAdapter`, `HotmartAdapter`,
  `MonetizzeAdapter`, `GoogleAnalyticsAdapter`, `GoogleAdsAdapter`)
  com os caminhos de arquivo reais onde vivem.

## Item 4 — Preservação de todo comportamento já aprovado ✅

Nenhuma linha de lógica de produção foi alterada em `bridge/execution/
navigator_real.py`, `bridge/execution/spend_guard.py`,
`atlas/escalation.py`, `core/security/sanitize.py` ou qualquer outro
módulo funcional desta rodada — apenas os 3 testes do item 1 (adição
de skip explícito) e o `README.md` (item 3, apenas documentação)
foram tocados. Confirmado: 1567 testes (mesma contagem da entrega
anterior), todos passando nas 3 execuções, incluindo os 90+ testes de
segurança/concorrência das rodadas anteriores do BLOCO FINAL 2.

## Arquivos alterados
- `tests/test_browser_executor.py` — skip explícito sem Chromium (2 testes)
- `tests/test_radar_real.py` — skip explícito sem Chromium (1 teste)
- `README.md` — seções "Uso" e "Escopo desta entrega" alinhadas ao
  código real; nenhuma mudança de comportamento de código

## Arquivos novos
Nenhum arquivo de teste novo nesta rodada — o escopo era corrigir
falsos-fails/desalinhamento, não adicionar cobertura nova.

## Pendências reais (honestamente declaradas)
- `pytest` continua indisponível neste ambiente
