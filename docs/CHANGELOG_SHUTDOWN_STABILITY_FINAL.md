# CHANGELOG — Microcorreção Final: Instabilidade Residual de Encerramento/Concorrência

Base: `MULTIVERSO_GROWTH_STUDIO_MASTER_V1_EXPANSION_V6_1_1_RESOURCE_CLEANUP_FINAL`
(1215 testes, 1215/1215 PASS). Corrige a instabilidade residual da
suíte completa (às vezes não encerra normalmente / trava de forma
intermitente).

## Causa raiz investigada e corrigida

Auditoria completa de threads/locks/executors/conexões em toda a base
(produção + testes) — nenhum deadlock de ordenação de locks
encontrado; todos os `RLock`/`Lock` usados corretamente, sem
aquisição aninhada de locks diferentes.

**Candidato real identificado**: `RadarWorker.stop()`/
`RadarWebWorker.stop()` usam `thread.join(timeout=X)` — se a operação
SQLite em andamento dentro do worker estiver bloqueada por contenção
de lock (podendo esperar até `busy_timeout`, que era 10000ms desde a
correção do travamento anterior), e o timeout de `stop()` usado em
alguns testes reais é **menor** (`stop(timeout=5.0)`, encontrado em 3
arquivos de teste), existe uma condição de corrida sensível a timing:
o `join()` pode desistir de esperar ANTES da thread do worker
realmente terminar, deixando-a genuinamente presa em segundo plano
por mais alguns segundos — exatamente o tipo de instabilidade
intermitente relatada (depende de como os timings coincidem a cada
execução).

**Correção aplicada**: `busy_timeout` reduzido de 10000ms para
**2000ms** — ainda generoso para a contenção residual que resta sob
modo WAL (que já elimina a maior parte dos casos de espera), mas com
margem de **2.5x** abaixo do MENOR `stop(timeout=...)` usado em
qualquer teste real da suíte.

## Validação da hipótese

Tentei reproduzir a condição de corrida diretamente — forçando
contenção real (múltiplas threads escrevendo concorrentemente no
mesmo banco) durante ciclos repetidos de start/stop de workers, tanto
com o valor NOVO (2000ms) quanto com o valor ANTIGO (10000ms) de
`busy_timeout`. Em nenhum dos dois casos consegui reproduzir uma
thread órfã neste ambiente (15+ ciclos sob estresse) — o que significa
que não posso provar definitivamente que esta era A causa exata do
relato, mas a correção aplicada é uma melhoria de segurança genuína,
bem fundamentada e mensurável (margem de segurança 2.5x versus
praticamente nenhuma margem antes), e o teste de regressão criado
verifica essa margem permanentemente.

## Resultado real — 3 execuções consecutivas, com verificação máxima

```
Execução 1: 1219 testes, 1219 PASS, 0 FAIL, 0 ERROR, 64.5s, exit_code=0, 0 arquivos órfãos
Execução 2: 1219 testes, 1219 PASS, 0 FAIL, 0 ERROR, 65.1s, exit_code=0, 0 arquivos órfãos
Execução 3: 1219 testes, 1219 PASS, 0 FAIL, 0 ERROR, 65.3s, exit_code=0, 0 arquivos órfãos
```

Todas as 3 execuções com `-W error::ResourceWarning` (avisos
promovidos a exceções — a checagem mais rigorosa possível). Variância
de duração muito baixa (64.5-65.3s) — sem outliers, sem sinais de
travamento intermitente.

Adicionalmente, 5 execuções anteriores (sem a checagem estrita, mas com
timeout de segurança de 120s cada) também terminaram limpas, com
variância ainda menor (39.7-42.3s).

**1219 testes** (1215 preservados + **4 testes novos** em
`tests/test_correction_final_shutdown_stability.py`).

## Arquivos alterados

- `core/models/db.py` — `busy_timeout` reduzido de 10000ms para
  2000ms em `get_connection()`/`get_autocommit_connection()`
- `tests/test_correction_final_shutdown_stability.py` (novo, 4 testes)

**Nenhuma funcionalidade aprovada foi alterada.** Nenhuma mudança de
arquitetura — `RadarWorker`/`RadarWebWorker` permanecem exatamente
como estavam (mesmas assinaturas, mesmo comportamento de
`start()`/`stop()`/`is_running()`). As correções de travamento (WAL),
resource cleanup, e continuidade/restart das rodadas anteriores foram
preservadas integralmente e revalidadas sem modificação.

## Teste de regressão específico (item explicitamente pedido)

`tests/test_correction_final_shutdown_stability.py`:
- `test_busy_timeout_has_safe_margin_below_shortest_worker_stop_timeout`
  — varre os PRÓPRIOS arquivos de teste em busca do menor
  `stop(timeout=...)` realmente usado (nunca assume um valor fixo) e
  confirma que `busy_timeout` mantém margem de segurança ≥2x — pega
  automaticamente qualquer reintrodução futura de um timeout de
  `stop()` perigosamente curto
- `test_radar_worker_never_orphaned_under_contention` /
  `test_radar_web_worker_never_orphaned_under_contention` — força
  contenção SQLite real (6 threads concorrentes) durante 8 ciclos de
  start/stop de cada worker, com `stop(timeout=5.0)` (o menor valor
  real usado na suíte) — nenhuma thread pode ficar viva depois que
  `stop()` retorna
- `test_worker_lifecycle_tests_terminate_cleanly_three_times_in_a_row`
  — roda os testes de ciclo de vida de worker em 3 SUBPROCESSOS
  separados e sequenciais, confirmando que cada um sempre termina
  sozinho, sem outliers de duração

## Diagnóstico honesto

Apesar de auditoria extensa (locks, threads, subprocessos,
descritores de arquivo, ordenação de locks cruzados) e testes de
estresse agressivos (contenção real forçada, múltiplos ciclos), não
consegui reproduzir um travamento genuíno neste ambiente — nem antes
nem depois da correção. A causa identificada (margem insuficiente
entre `busy_timeout` e o timeout de `stop()` de alguns testes) é a
explicação mais forte e tecnicamente sólida disponível, e a correção
aplicada é uma melhoria de segurança real e mensurável, mesmo sem
prova definitiva de que era exatamente a causa do relato original.

## Pendências reais
- `pytest` continua indisponível neste ambiente.
- `ClaimRegistry` ainda não é persistido (pendência já declarada em
  rodadas anteriores).
- Módulo YouTube e Omnichannel Campaign Intelligence não implementados,
  conforme instruído em todas as rodadas.
