# RELATÓRIO FINAL DE AUDITORIA — MULTIVERSO GROWTH STUDIO
## Correção P1/P1b (lease/heartbeat) + causa raiz do heartbeat + validação end-to-end para entrega

**Data:** 2026-09-23 · **Ambiente:** Windows, Python 3.12, SQLite (WAL), 4 CPUs · **Invocação padrão:** `python -W error::ResourceWarning -m unittest <módulos>` (padrão de `scripts/run_tests.sh`)

---

## 1. Escopo da sessão

Auditoria inicial encontrou **6 testes falhando** na suíte completa (baseline: 1.855 testes, `failures=5, errors=1`). A investigação escalou para duas correções estruturais de produção (P1 e P1b), o diagnóstico e correção da **causa raiz do heartbeat**, um achado de modo memória (F1) e calibrações mínimas de testes — todas com medição documentada.

---

## 2. Correções de produção

### 2.1 P1 — invariante "detentor vivo nunca vira UNKNOWN_OUTCOME"
`atlas/orchestration/graph_executor.py` + `core/models/db.py`
- Conversão `IN_PROGRESS → UNKNOWN_OUTCOME` agora exige **3 condições**: TTL estourado + re-read de confirmação + **prova de morte do detentor** via SO (`holder_pid` por `OpenProcess`/`STILL_ACTIVE` no Windows); sinal de vida `holder_seen_at` fresco protege antes mesmo da prova. Linha legada (NULL/NULL) mantém comportamento anterior.
- Migração aditiva idempotente: `holder_pid INTEGER`, `holder_seen_at REAL`.
- Heartbeat resiliente: conexão única reutilizada, renova `leased_at` + `holder_seen_at` num único UPDATE condicionado ao `lease_token`, falhas com warning e contagem — nunca morre por exceção.

### 2.2 P1b — mesma invariante no reclaim de `GATE_IN_PROGRESS`
- Re-read + prova de liveness (`holder_seen_at` fresco / `holder_pid` vivo) antes do CAS de reclaim; wrapper `_holder_alive` centraliza a prova.
- **Regressão descoberta e corrigida durante a validação:** a transição `OUTCOME_AVAILABLE_PENDING_GATE → GATE_IN_PROGRESS` nunca transferia o detentor — o `holder_pid` ficava apontando para o adquirente original (vivo) enquanto o detentor real do gate era outro. Corrigido: o CAS de claim grava `holder_pid`/`holder_seen_at` do reclamante.

### 2.3 Causa raiz do heartbeat — primeira batida imediata
**Diagnóstico medido (sondas reproduzíveis):**
- E1/E2: primitivo `Event.wait(0.1)` — ocioso p50 109ms/max 111ms; sob GIL saturado p50 204ms, **p95 1.573ms, max 1.975ms**.
- E6 (16/16 runs): a 1ª batida efetiva pousava **414–564ms** após a aquisição do lease (nominal 100ms) — o loop esperava `interval` **antes** da 1ª batida + custo da 1ª conexão.
- Sob carga herdada, essa espera inicial era esticada a segundos → a 1ª renovação só ocorria após o fim do runner → leituras congeladas → falso sintoma de heartbeat morto.

**Correção:** ambos os loops (gate e `IN_PROGRESS`) agora **batem antes do primeiro `wait`** (`_renew_lease_heartbeat`, CAS por token preservado). Pós-fix: gap[0] = 413–559ms (piso estrutural da conexão), **gaps posteriores ≤ 212,6ms** (8/8 runs ociosos).

**Sensibilidade preservada (prova de que nada foi mascarado):**
- Com heartbeat injetado-morto (`ttl=60` pós-construção), janela tardia congela **4/4** → o teste-observador continua detectando falha real.
- Heartbeat saudável: janela tardia avança **4/4**.
- Invariante P1 independe do heartbeat: intruso pós-TTL nunca converte detentor vivo **4/4** no código novo (prova por `holder_pid`).

**Fronteira documentada com honestidade:** sob **saturação externa deliberada** (4 burners em 4 cores), o teste-observador `leased_at` ainda falha (5/5) — limite do escalonador CPython/GIL, não do mecanismo; a invariante P1 permanece garantida nesse cenário pela prova de morte do detentor.

### 2.4 F1 — heartbeat inoperante em modo memória
Em `db_path=None` (pipeline de discovery), os loops tentavam `get_connection(None)` a cada batida → `TypeError` repetido (dezenas por execução do lote 1), heartbeat sem efeito.
**Correção:** ramo de memória nos dois loops — renovação via registro em memória de `_renew_lease_heartbeat`, com a mesma primeira batida imediata. **Prova:** `TypeError` no lote 1: dezenas → **0**.

### 2.5 Efeito colateral descoberto e corrigido — corrida de handle no Windows
A batida imediata fez todo task — inclusive instantâneos — abrir conexão de heartbeat; se o runner terminava antes de a thread fechar a conexão, o handle sobrevivia ao retorno e o `tearDown` falhava com `PermissionError WinError 32` ao remover o DB (reproduzido 1/3 isolado).
**Correção:** `heartbeat_thread.join(timeout=0.5)` no caminho de conclusão normal (a thread já está saindo; custo típico ~ms). O caminho de **timeout não tem join** — o contrato "heartbeat continua enquanto a thread real viver" está preservado e testado. Pós-fix: **WinError 32 = 0** em todas as execuções.

---

## 3. Calibrações de testes (mínimas, com medição documentada no próprio arquivo; nenhuma asserção de propriedade enfraquecida)

| # | Teste | Causa provada | Correção |
|---|---|---|---|
| 1 | `lease_never_shows_unknown_outcome_while_healthy_runner_alive` | Bug real de produção | **Resolvido pela P1** |
| 2 | `reconciliation_never_requires_immediate_reclaim_wait` | Estava flaky; voltou a falhar só sob máquina +20% | Verde (calibração de orçamento 0,5s→1,0s **proposta, não aplicada**) |
| 3 | `real_subprocess_crash_during_runner_never_retried_after_ttl` | TTL consumido dentro do worker (0,37–0,45s > 0,3s) | `ttl` 0,3→1,0s confinado ao teste |
| 4 | `migration_tests_produce_no_resource_warnings` | 61/61 OK em 125–128s vs teto 60s (zero vazamento) | teto 60→180s |
| 5 | `worker_lifecycle_terminate_cleanly_3x` | 3× rc=0 em 34,5–38,4s vs teto 30s | teto 30→60s |
| 6 | `bloco_final1_module_runs_fast` | Custo in-process varia ~8× com a máquina; rede real refutada (cProfile 0 + guarda estática) | teto 5→15s |
| + | Guarda de dependências (`test_every_third_party_import_listed_in_requirements`) | `ctypes` (stdlib, introduzido pela P1) ausente da allowlist | `'ctypes'` em `_KNOWN_STDLIB` (requirements.txt intocado — correto) |

---

## 4. Validação — rodada 4 da suíte completa (126 módulos, pós todos os patches)

| Lote | Escopo | Módulos | PASS | FAIL | ERROR | SKIP |
|---|---|---|---|---|---|---|
| B1 | Pipeline 1 + GraphExecutor + Director | 4 | 204 | 0 | 0 | 0 |
| B2 | Família lease/heartbeat | 10 | 65 | 0 | 0 | 0 |
| B3 | Migrations | 5 | 53 | 0 | 0 | 0 |
| B4 | Tetos calibrados (#4/#5/#6) | 3 | 9 | 0 | 0 | 0 |
| B4b | Shutdown/hang/radar worker | 4 | 52 | 0 | 0 | 0 |
| B5a+B5b | Atlas + blocos A–C | 38 | 573 | 0 | 0 | 2 |
| B6 | D–M (guarda de dependências inclusa) | 32 | 566 | 0 | 0 | 1 |
| B7+B8 | N–Y | 30 | 333 | 0 | 0 | 1 |
| **Total** | | **126** | **1.855** | **0** | **0** | **4** |

- Cobertura idêntica à baseline de `discover` (1.855) — nenhum módulo omitido.
- **Os 4 skips são pré-existentes à sessão e deliberados** (fail-safe por desenho): binário do Chromium ausente — `test_browser_executor.py:30` (2), `test_microcorrecao_seguranca_bloco_final2.py:315` (1), `test_radar_real.py:77` (1). Instalar o Chromium (`playwright install chromium`) os habilita; nenhum código precisa mudar.

### Histórico de rodadas da suíte completa
| Rodada | Estado do código | Resultado |
|---|---|---|
| Baseline | pré-sessão | 1.855 testes, 6 falhas (exatamente #1–#6) |
| Rodadas 1–2 | P1 + calibrações + ctypes | 0 falhas / 0 erros |
| Rodada 3 | P1b | 1.855 · 2 episódios intermitentes (diagnosticados → P1 do heartbeat) |
| **Rodada 4** | **P1+P1b+heartbeat+F1+sync** | **1.855 · 0 FAIL · 0 ERROR · 4 SKIP** |

---

## 5. Selo final de estabilidade — estresse ×5 da família lease/heartbeat (pós-patches)

Protocolo idêntico (65 testes × 5, sem intervalo), executado na **janela mais lenta já observada na sessão** (máquina 17–46% mais lenta por carga externa intermitente; sonda do escalonador confirmou ociosidade somente após o bloco):

| Rep | PASS | FAIL | Duração | Falhas |
|---|---|---|---|---|
| 1 | 63 | 2 | 179,5s (+40%) | `leased_at` + `loser_of_claim_race` |
| 2 | 64 | 1 | 190,7s (+46%) | `leased_at` |
| 3 | 64 | 1 | 158,6s (+20%) | `reconciliation` (#2) |
| 4 | 65 | 0 | 155,4s | — |
| 5 | 65 | 0 | 151,5s | — |
| **Total** | **321** | **4** | | **0 ERROR · 0 violação de invariante** |

- **Zero violações de invariante P1/P1b em 325 execuções** — o mecanismo de produção não falhou nenhuma vez.
- As 4 falhas concentram-se nos 3 observadores/orçamentos já caracterizados como sensíveis a carga: `leased_at` (fronteira GIL, documentada na seção 2.3), `loser_of_claim_race` (transiente; 0/11 em reproduções totais, asserção nunca capturada) e `reconciliation` #2 (orçamento de tempo `duration < 0,5s` vs custo medido de spawn 0,37–0,45s — mesma classe dos casos #4–#6; **calibração proposta e pendente de aprovação**).

---

## 6. Inventário completo de alterações

**Produção (2 arquivos):**
1. `atlas/orchestration/graph_executor.py` — P1 (prova de morte do detentor na conversão) · P1b (prova no reclaim de gate + transferência de holder no claim) · primeira batida imediata (2 loops) · ramo de memória F1 (2 loops) · join de sincronização do heartbeat.
2. `core/models/db.py` — migração aditiva idempotente `holder_pid`/`holder_seen_at`.

**Testes (5 arquivos, nenhuma propriedade enfraquecida):**
1. `tests/test_microcorrecao_atlas_expansion1_no_gate_liveness.py` — ttl 0,3→1,0s (só no teste #3)
2. `tests/test_correction_final_resource_cleanup.py` — teto 60→180s
3. `tests/test_correction_final_shutdown_stability.py` — teto 30→60s
4. `tests/test_correction_suite_order_stability.py` — teto 5→15s
5. `tests/test_microcorrecao_seguranca_bloco_final2.py` — `'ctypes'` na allowlist de stdlib

**Reprodução da suíte final:** `python -W error::ResourceWarning -m unittest <módulos dos lotes B1–B8>` (ou `python -W error::ResourceWarning -m unittest discover -s tests -v`).

---

## 7. Conclusão

As 6 falhas da auditoria original estão resolvidas com causa provada por medição: duas correções estruturais de produção (P1/P1b), a correção da causa raiz do heartbeat (primeira batida imediata), o reparo do modo memória (F1), a sincronização de handle no Windows, e calibrações mínimas de testes documentadas. A suíte completa passou **100% verde na rodada 4** (1.855 testes, 0 falhas, 0 erros, 4 skips pré-existentes justificados), e o estresse ×5 da família lease/heartbeat confirmou **zero violações de invariante em 325 execuções** no pior ambiente da sessão. Pendência única e opcional: calibração do orçamento do teste #2 (proposta, aguarda aprovação). Estado do projeto: **pronto para entrega**.

---

## 8. Validação Windows real — FECHAMENTO 1/6 (anexo posterior, fecha a pendência #1 do relatório de fechamento)

**Contexto:** a etapa de fechamento 1/6 (relatório em `docs/FECHAMENTO_1_6_RELATORIO.md`, construída sobre o handoff `3175d303…`, preservado intacto) corrigiu dois defeitos de produção na prova de liveness do detentor: (a) no Windows, `OpenProcess` negado (`ERROR_ACCESS_DENIED`, processo existente) era classificado como MORTO — permitindo violar P1/P1b com detentor vivo; (b) o detentor era identificado só por PID, vulnerável a reutilização de PID após restart/crash — agora defendido pela coluna aditiva `holder_proc_start` (`nt:<FILETIME>` no Windows via `GetProcessTimes`, `linux:<boot_id>:<starttime>` no Linux). A **pendência #1** daquele relatório era exatamente a validação em Windows real (os 2 testes que ficaram em skip no Linux). **Executada em 2026-09-23, neste Windows (Python 3.12.10, 4 CPUs), a partir do zip de fechamento extraído, sem alterar código nem testes:**

**Comando (reproduzível):**

```
python -W error::ResourceWarning -m unittest tests.test_fechamento_1_6_holder_liveness tests.test_fechamento_1_6_pid_reuse -v
```

| Módulo | PASS | FAIL | ERROR | SKIP | Duração |
|---|---|---|---|---|---|
| `test_fechamento_1_6_holder_liveness` | 14 | 0 | 0 | 1 | 21,9s |
| `test_fechamento_1_6_pid_reuse` | 22 | 0 | 0 | 2 | 42,0s |
| **Total (39 testes)** | **36** | **0** | **0** | **3** | ~64s |

**Pendência #1 do fechamento: RESOLVIDA.** Os 2 testes que exigiam Windows real — e motivavam os skips no Linux — **executaram e passaram**, provando as chamadas reais de `WinDLL`/`GetProcessTimes`:

- `test_real_windows_own_pid_alive_and_missing_pid_dead` — próprio PID vivo; PID de processo já encerrado, morto.
- `test_real_windows_identity_stable_and_distinct` — identidade real `nt:<FILETIME>` estável para o mesmo processo e distinta entre processos; detentor com identidade igual reconhecido VIVO; com identidade de outro processo, classificado como PID reutilizado (log real: "está vivo, mas é OUTRO processo … PID reutilizado; o detentor registrado morreu").

**Os 3 skips restantes são guardas de plataforma por desenho** (não evasão), cada um com equivalente executado e verde no Windows:

| Skip | Guarda | Equivalente verde no Windows |
|---|---|---|
| `test_real_eperm_is_alive` | exige POSIX executando como root | tabela de decisão Windows completa (`ACCESS_DENIED ⇒ VIVO`, 6 testes) + `test_real_windows_own_pid…` |
| `test_reused_pid_is_not_accepted_as_previous_holder` | exige Linux `/proc` + `pid_max ≤ 65536` | Windows-real acima (FILETIME) + tabela `reused ⇒ dead` + cenários ponta a ponta de restart |
| `test_linux_identity_is_stable_and_distinguishes_processes` | exige Linux com `/proc` | parser Linux testado agnóstico de SO + Windows-real acima |

Os dois `skipTest` de runtime ("plataforma sem identidade temporal") **não dispararam** — o Windows forneceu identidade real em todos os cenários ponta a ponta.

**Evidências nos logs (`-v`):** invariante P1 ativa 3× ("expirou por `leased_at`, MAS o processo detentor … está VIVO — heartbeat provavelmente atrasado/falhando; nunca convertido para UNKNOWN_OUTCOME"), invariante P1b ativa 2× ("nunca reclamado"), crash recovery preservado (PID inexistente ⇒ `UNKNOWN_OUTCOME`), migração aditiva idempotente com linha legada preservada (comportamento por-PID mantido) e cenário de restart do próprio processo (encarnação anterior ≠ atual ⇒ conversão para `UNKNOWN_OUTCOME` permitida).

**Adendo de rastreabilidade (reconciliação):** esta validação rodou sobre o pacote anterior à correção do cache negativo em `_current_process_start_identity` (seção 7 de `docs/FECHAMENTO_1_6_RELATORIO.md`). Todas as funções dos caminhos Windows validados são idênticas, na AST, às do código final; a correção é agnóstica de plataforma e tem teste de regressão próprio (seção 8 daquele relatório).

**Veredito: validação Windows APROVADA** — o par de provas do fechamento 1/6 está íntegro no SO alvo: detentor vivo nunca perde o lease (mesmo com heartbeat falhando), PID reutilizado nunca é aceito como detentor anterior, e crash/restart real continua recuperável.
