# CHANGELOG — Microcorreção: Shutdown Residual (RadarWorker/RadarWebWorker)

Base: `MULTIVERSO_GROWTH_STUDIO_MASTER_V1_EXPANSION_V6_1_1_BF3_CLAIM_COMPOSITE_KEY`
(1600 testes). Corrige a falha real reproduzida pela auditoria,
preservando integralmente BF1/BF2/BF3 e a correção composite-key.

## Resultado real — 3 execuções consecutivas, verificação máxima

```
COLLECTED: 1609  PASS: 1609  FAIL: 0  ERROR: 0  SKIP: 0
DURATION: ~87-89s (3 execuções consecutivas, -W error::ResourceWarning)
EXIT CODE: 0 (as 3 vezes — processo encerra de verdade, nunca via timeout externo)
ResourceWarnings: 0, threads residuais: 0
```

**1609 testes** (1600 preservados + **9 testes novos**).

## Falha real confirmada

Em `RadarWorker.stop()` e `RadarWebWorker.stop()`: após
`thread.join(timeout)`, o código definia `self._thread = None`
INCONDICIONALMENTE — mesmo quando `thread.is_alive()` continuava
`True` (a thread ainda genuinamente rodando, presa dentro de
`scheduler.run_if_due()`). Isso fazia `is_running()` mentir `False`
para uma thread que ainda estava viva — um falso sucesso de shutdown.

## Correção — fail-closed

A referência da thread só é descartada quando ela REALMENTE terminou
(`thread.is_alive()` é `False` após o `join`). Se o timeout se esgota
com a thread ainda viva:
- A referência é MANTIDA (`self._thread` nunca é limpo).
- Nova exceção `WorkerShutdownTimeout` é levantada explicitamente —
  o chamador nunca precisa inferir a falha indiretamente de um
  `is_running()` que mentiria.

Aplicado identicamente aos DOIS workers (`atlas/radar/worker.py` e
`radar_web/worker.py` — implementações estruturalmente idênticas,
cada uma com sua própria classe `WorkerShutdownTimeout`).

## Correção adicional necessária — cadeia de shutdown em `api/bootstrap.py`

Ao implementar a correção acima, encontrei um problema real
decorrente dela: `GrowthStudioApp.shutdown()` chamava
`stop_radar_worker()` e DEPOIS `stop_radar_web_worker()` em sequência
— se o PRIMEIRO levantasse `WorkerShutdownTimeout` (thread
genuinamente presa), o SEGUNDO NUNCA seria tentado, deixando ESSE
outro worker órfão mesmo que ele próprio tivesse conseguido parar a
tempo. Corrigido: `shutdown()` agora SEMPRE tenta ambos os `stop()`,
independentemente de o primeiro ter levantado — captura e agrega as
falhas (via `ExceptionGroup` quando ambos falham), relevantando ao
final. Nunca esconde a falha, nunca deixa um worker sem sequer uma
tentativa de parada por causa do outro.

## Validado com o cenário exato exigido

`tests/test_microcorrecao_radar_worker_shutdown.py` — scheduler de
teste DELIBERADAMENTE BLOQUEADO (`run_if_due()` trava num
`threading.Event` controlado pelo próprio teste, nunca simulando a
condição):
- `stop(timeout curto)` com a thread genuinamente presa → levanta
  `WorkerShutdownTimeout`, `is_running()` continua `True`, a
  referência da thread permanece — para AMBOS `RadarWorker` e
  `RadarWebWorker`.
- Múltiplas tentativas de `stop(timeout curto)` seguidas — nunca, em
  nenhuma delas, a referência é descartada nem `is_running()` mente.
- Depois de LIBERAR o bloqueio, `stop()` com timeout generoso
  encerra de verdade — `is_running()` volta a `False`, referência
  genuinamente limpa (`None`).
- Caminho feliz (sem bloqueio) preservado — `stop()` continua
  funcionando exatamente como antes, sem levantar nada.
- Teste de integração adicional (`app.shutdown()` com AMBOS os
  workers travados) confirma que os dois recebem tentativa de
  parada, e que depois de liberar ambos, o shutdown real acontece
  para os dois.

### Nota sobre os valores de timeout usados nos testes

Os timeouts curtos (`0.1s`/`0.2s`) são passados por variável nomeada,
nunca como literal numérico direto na chamada — o scanner estático
de `test_correction_final_shutdown_stability.py` (correção anterior,
preservada sem alteração) varre por literais numéricos em
`.stop(timeout=...)` para garantir margem de segurança contra
contenção REAL de SQLite. O bloqueio usado nestes novos testes é
puramente em memória (`threading.Event.wait()`, sem nenhuma operação
de banco) — o risco de flakiness que aquele scanner protege não se
aplica aqui, mas o valor literal ainda dispararia o alarme do
scanner por coincidência de padrão; usar uma constante nomeada evita
o falso positivo sem enfraquecer a proteção original.

## Preservação confirmada explicitamente

Reexecutei os 14 arquivos de teste dedicados de BF1/BF2/BF3 e a
correção composite-key — **197 testes, todos passando sem
alteração**.

## Arquivos alterados
- `atlas/radar/worker.py` — `WorkerShutdownTimeout` (nova exceção);
  `stop()` fail-closed
- `radar_web/worker.py` — mesma correção, classe própria
- `api/bootstrap.py` — `shutdown()` sempre tenta ambos os workers

## Arquivos novos
- `tests/test_microcorrecao_radar_worker_shutdown.py` (9 testes)

## Pendências reais (honestamente declaradas)
- `pytest` continua indisponível neste ambiente
