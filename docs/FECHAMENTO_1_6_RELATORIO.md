# FECHAMENTO TÉCNICO 1/6 — MULTIVERSO GROWTH STUDIO

**Data:** 2026-09-23 · **Baseline:** `multiverso_growth_studio_handoff_2026-09-23.zip` (SHA-256 `3175d303af7c1e732c2f6a118aad274be393448507b6ab69419ff8fa7451bdcb`, preservado intacto)
**Ambiente de validação:** Linux (sandbox), Python 3.12.3, 1 CPU, Chromium disponível, sem rede.
**Invocação:** `python -W error::ResourceWarning -m unittest discover -s tests -t . -v`

## 1. Resultado final

| Testes | PASS | FAIL | ERROR | SKIP | RC | Duração |
|---|---|---|---|---|---|---|
| 1.895 | 1.893 | 0 | 0 | 2 | 0 | 131,2 s |

Os 1.895 testes correspondem aos 1.855 originais mais 40 novos (15 em `test_fechamento_1_6_holder_liveness.py` e 25 em `test_fechamento_1_6_pid_reuse.py`). Nenhum teste foi removido. O log completo não contém nenhum `ResourceWarning`.

**Skips (2), ambos novos e deliberados:** `test_real_windows_own_pid_alive_and_missing_pid_dead` e `test_real_windows_identity_stable_and_distinct` exigem Windows real. As decisões do ramo Windows são cobertas em qualquer SO por um `kernel32` simulado, mas as chamadas reais de `WinDLL`/`GetProcessTimes` só podem ser executadas em Windows.

**Os 4 skips do relatório anterior (Chromium ausente) não ocorreram:** neste ambiente o Chromium está disponível, então os 4 testes executaram e passaram.

## 2. Causas-raiz e correções

### 2.1 Invariante single-host (ponto 1)
A camada de lease é **exclusivamente single-host** por arquitetura, e não por escolha desta camada:
- o banco é um arquivo SQLite aberto em modo WAL (`core.models.db.get_connection`);
- o WAL exige memória compartilhada (`-shm`) entre os processos e não funciona sobre sistema de arquivos de rede, uma restrição documentada do SQLite;
- todo acesso à tabela de leases passa por `core.models.db`;
- o servidor faz bind em `127.0.0.1`.

A invariante foi documentada no docstring de `GraphExecutor` e é guardada por testes que verificam: conexões em WAL, ausência de conexão direta em `graph_executor.py` (verificação por AST, ciente de aliases) e identidade local do detentor.

### 2.2 Windows × POSIX, acesso negado (ponto 2), defeito de produção
**Defeito reproduzido no baseline:** `OpenProcess` negado (`ERROR_ACCESS_DENIED`, processo existente) era classificado como **MORTO**. Isso permitia violar P1 (conversão para `UNKNOWN_OUTCOME`) e P1b (reclaim do gate) com o detentor vivo. No POSIX, o caso equivalente (`EPERM`) já era tratado como vivo. Havia ainda dois defeitos associados:
- `ctypes.windll.kernel32` era usado sem `use_last_error`, de modo que o `GetLastError` não era lido de forma confiável;
- sem `restype`, o `HANDLE` de 64 bits era tratado como `c_int`.

**Correção:** instância própria de `WinDLL("kernel32", use_last_error=True)` com assinaturas declaradas. A tabela de decisão ficou:
- acesso negado → vivo;
- PID inexistente → morto;
- handle válido mas `GetExitCodeProcess` falhou → vivo.

**Provas:**
- tabela de decisão completa;
- P1 e P1b de ponta a ponta com consulta negada;
- `EPERM` real no POSIX (filho rebaixado para `nobody` consulta o pai root → vivo);
- recuperação de crash preservada (PID inexistente → `UNKNOWN_OUTCOME`).

### 2.3 PID reutilizado após restart/crash (item 2), defeito de produção
**Defeito:** o detentor era identificado apenas pelo PID. Se o SO entregasse o mesmo PID a outro processo, o processo alheio era aceito como o detentor anterior e o lease ficava preso indefinidamente. Em contêiner, o caso típico é o próprio app reiniciado com o mesmo PID.

**Correção:** coluna aditiva `holder_proc_start TEXT`, que guarda a identidade temporal do processo detentor como string opaca comparada por igualdade exata:
- **Linux:** `linux:<boot_id>:<starttime>`, com `starttime` em ticks desde o boot (campo 22 de `/proc/<pid>/stat`). Não é hora de parede, porque um ajuste de relógio mudaria o `btime` e faria um processo vivo parecer outro. O `boot_id` distingue reinicializações da máquina.
- **Windows:** `nt:<FILETIME de criação>`, via `GetProcessTimes`.
- **Outros POSIX (sem `/proc`):** sem suporte. O comportamento volta a ser a verificação apenas por PID, como antes.

A identidade é gravada nos três pontos de posse: aquisição, claim do gate e reclaim. A decisão é tomada por `_holder_identity_alive`:

| Situação | Decisão |
|---|---|
| PID inexistente | morto |
| identidade registrada NULL (linha legada) | só o PID decide (comportamento anterior) |
| identidade atual ilegível (acesso negado, corrida com o fim do processo) | vivo (sem prova de troca, P1 prevalece) |
| identidades iguais | vivo |
| identidades diferentes | PID reutilizado: o detentor registrado morreu |

**Compatibilidade:** a migração é aditiva e idempotente, e linhas existentes recebem NULL e mantêm o comportamento anterior. O protocolo do lease (status, tokens, generation, CAS) não mudou. A única mudança de protocolo é a gravação de uma coluna a mais nos três pontos de posse.

### 2.4 Tetos dos testes #4, #5 e #6 (ponto 3)
| # | Achado medido | Correção |
|---|---|---|
| 4 | **A guarda era cega.** No Python 3.12, `sqlite3` não emite `ResourceWarning` (só a partir do 3.13), e um aviso em destrutor sob `-W error` vira "exception ignored" com `rc=0`. Mutante (conexão nunca fechada em `init_db`): comando antigo `rc=0` (2/2). | `tests/_helpers/strict_resource_runner.py` rastreia o `close()` das conexões de produção e captura exceções ignoradas: `rc=2` com o mutante, `rc=0` no código real. O teto de 180 s só limita a espera por travamento (medido: 2,1 s aqui, 126 s no Windows). |
| 5 | **O teto é válido.** Mutante (thread não-daemon eterno): `TimeoutExpired` em 60 s. Medido aqui: ~7,4 s por subexecução. | nenhuma |
| 6 | **O teste era cego.** O cenário estimado de rede real (~12,1 s) fica abaixo do teto de 15 s, e sem rede a falha é instantânea. Mutante (`mock` de `urlopen` removido): este teste e a guarda estática passaram (2/2). | Sentinela determinística no nível do socket: o mutante é detectado e a tentativa exata é registrada (2/2). |

### 2.5 Teste #2 (ponto 4)
A calibração proposta (0,5 → 1,0 s) **teria mascarado** a regressão. O spawn fica fora da janela cronometrada, e o custo real medido é de ~15 ms (p50; máximo 20 ms em 15 execuções). O mutante `sleep(TTL)` no ramo `COMPLETED` passava no teste original em 3/3. Correção: TTL do teste = 5,0 s e orçamento de 1,0 s. Mutante detectado em 3/3.

## 3. Evidências por mutação: PID reutilizado
- **Reutilização real de PID:** o detentor morre por crash e o espaço de PIDs é ciclado até o SO entregar o mesmo PID a outro processo. Pré-condição verificada: pelo PID, o processo alheio parece vivo, mas a identidade diverge. Resultado: `UNKNOWN_OUTCOME`, e o lease não fica preso.
- **Mutante com a semântica do baseline (apenas PID):** 6 testes falham, incluindo o de reutilização real, os de restart com o mesmo PID (P1 e P1b) e o de Windows.
- **Mutantes agressivos, todos detectados:**
  - identidade ilegível → morto;
  - linha legada → morto;
  - identidade não gravada na aquisição.
- **Processo realmente vivo** com identidade real registrada e heartbeat parado: nunca convertido nem reclamado (P1 e P1b).

## 4. Static guard: ponto cego documentado, não contornado
`test_every_function_that_opens_a_raw_connection_closes_it_or_hands_it_off` percorre as funções via AST, mas **conta aberturas e fechamentos por busca textual**. Consequências:
1. **Falso positivo:** o padrão escrito em strings ou comentários conta como abertura. Isso aconteceu com um teste desta etapa. A primeira correção, uma concatenação de strings, contornava a guarda e foi **revertida**. A correção definitiva fez o teste verificar chamadas reais via AST, o que foi provado mais forte: detecta 3 de 3 formas de conexão direta, contra 1 de 3 da versão textual.
2. **Falso negativo (ponto cego):** conexões abertas por alias (`x = sqlite3.connect; x(...)`, `import sqlite3 as s`) não são vistas.

A guarda não foi alterada. Mutantes confirmam que ela continua detectando vazamento real em produção (`core/jobs/queue.py`) e nos arquivos de teste desta etapa, que não receberam nenhuma isenção.

**Nenhuma correção desta etapa depende do ponto cego para passar.** Uma auditoria independente, via AST e ciente de aliases, sobre todos os arquivos alterados classifica toda abertura de conexão como fechada explicitamente ou devolvida ao chamador. O único caso invisível à guarda é `_tracked_connect`, no runner estrito, um hand-off legítimo que devolve a conexão.

**Correção de um vazamento próprio:** a regressão intermediária revelou um `ResourceWarning` de pipe não fechado em `GenuinePidReuseTests`, que não derrubou o `RC` justamente pelo ponto cego do #4. O runner estrito o confirmou (`rc=3`). O pipe passou a ser fechado explicitamente, e o runner estrito voltou a `rc=0` (2/2).

## 5. Arquivos
**Produção (2):** `atlas/orchestration/graph_executor.py` e `core/models/db.py`.

**Testes alterados (3), todos com sensibilidade maior:**
- `tests/test_microcorrecao_atlas_expansion1_no_gate_liveness.py`
- `tests/test_correction_final_resource_cleanup.py`
- `tests/test_correction_suite_order_stability.py`

**Novos (5):**
- `tests/test_fechamento_1_6_holder_liveness.py`
- `tests/test_fechamento_1_6_pid_reuse.py`
- `tests/_helpers/strict_resource_runner.py`
- `tests/_helpers/pid_reuse_occupier.py`
- `docs/FECHAMENTO_1_6_RELATORIO.md`

## 6. Pendências
1. ~~**Validação em Windows real**~~ **RESOLVIDA** (ver seção 8 deste relatório e seção 8 de `docs/AUDITORIA_FINAL_P1_HEARTBEAT_ENTREGA.md`): 36 PASS · 0 FAIL · 0 ERROR · 3 SKIP (guardas POSIX/Linux) em Windows real, Python 3.12.10. As chamadas reais de `WinDLL`/`OpenProcess`/`GetProcessTimes` foram executadas e aprovadas.
2. **Tempos no Windows:** os tempos medidos aqui (Linux, 1 CPU) não substituem os do Windows. Os tetos #4 e #5 foram mantidos com base nas medições do Windows.
3. **POSIX sem `/proc` (ex.: macOS) — cobertura futura, NÃO bloqueadora:** o macOS não é plataforma-alvo (a operação é Windows, validado; o desenvolvimento/CI desta etapa é Linux, validado). Nele, a defesa contra PID reutilizado não se aplica e o comportamento é EXATAMENTE o anterior (prova apenas por PID) — sem regressão; o fallback está documentado e testado (`test_posix_without_proc_falls_back_to_pid_only`).
4. **Ponto cego da static guard (aliases):** documentado na seção 4. Uma versão por AST exigiria mudança separada, com a mesma prova por mutação.
5. **Defesa em profundidade não aplicada:** `_resolve_lease` cerca por `lease_token`, mas não por `status`. Um `UNKNOWN_OUTCOME` só poderia ser sobrescrito se P1 fosse violada, o que as provas desta etapa impedem.
6. **Revisão ainda por fazer:** heartbeat, F1, `join`, concorrência, timeout/retry, idempotência e persistência foram lidos nos pontos tocados, mas não passaram por revisão sistemática com mutação.

## 7. Execução final dos 2 módulos (VM Linux nova, kernel 6.18) e defeitos adicionais

A primeira execução nesta VM, na ordem `holder_liveness → pid_reuse`, terminou com `RC=0`, mas com **5 skips**, 3 deles inesperados. Isso levou a dois achados:

1. **Defeito de PRODUÇÃO, o cache negativo:** `_current_process_start_identity()` guardava `None` em cache. Uma única falha transitória na primeira leitura desligava em silêncio, pelo resto da vida do processo, a defesa contra PID reutilizado: toda aquisição gravava NULL, a semântica do baseline.
   - Prova direta: com falha transitória na 1ª leitura, a 2ª também devolvia `None`, embora a leitura sem cache funcionasse.
   - Gatilho nos testes: uma aquisição feita sob o Windows simulado com acesso negado envenenava o cache (o `INSERT` avalia o parâmetro mesmo quando falha por conflito de chave).
   - **Correção:** o cache guarda só identidades obtidas com sucesso.
2. **Defeitos de TESTE:**
   - Os skips "plataforma sem identidade temporal" mascaravam a regressão numa plataforma que tem suporte. Passaram a ser **falha** no Linux com `/proc` e no Windows. O mutante com a produção antiga agora gera **3 FAIL**, e não skips.
   - O teste de reutilização real assumia que qualquer PID volta pelo ciclo. No kernel Linux, após o wrap, a alocação recomeça em 300, e o detentor recebera o PID 115, impossível de reutilizar. O teste agora avança a alocação para PID ≥ 300 antes do crash e verifica essa pré-condição.
   - Foi adicionado um teste de regressão do cache negativo.

**Resultado após as correções:** 40 testes, 38 PASS, 0 FAIL, 0 ERROR e 2 SKIP (Windows real), nas duas ordens de execução e no runner estrito. Reutilização real de PID em 3 de 3 rodadas (PIDs 366, 374 e 382).

**Os 2 testes de Windows real não podiam ser executados no ambiente Linux.** Foram executados depois em Windows real e aprovados (seção 8).

## 8. Reconciliação com a validação Windows real (baseline `b067f305…`)

O pacote validado no Windows (`multiverso_growth_studio_handoff_2026-09-23_windows_validado.zip`, SHA-256 `b067f305f789b68bf2fb51910b1d26613457b5545cc98d4eed18636038b3753d`) foi montado a partir do pacote de fechamento **anterior** à correção do cache negativo (seção 7). Consequências verificadas:

1. **A validação Windows vale integralmente para o código final.** As 8 funções dos caminhos de liveness e identidade são idênticas, na AST, entre o código validado no Windows e o código final: `_win_kernel32`, `_win_process_alive`, `_win_process_start_identity`, `_process_start_identity`, `_linux_process_start_identity`, `_FILETIME`, `_holder_identity_alive` e `_holder_process_alive`. A única função que difere é `_current_process_start_identity`, correção agnóstica de plataforma com teste de regressão próprio.
2. **O baseline Windows-validado continha o defeito de produção da seção 7**, provado sobre ele: após uma falha transitória, a 2ª leitura também devolve `None`, e a defesa fica desligada. Os testes daquela versão o mascaravam: `OK` com 5 skips no Linux, na ordem `holder_liveness → pid_reuse`.
3. **Reconciliação aplicada** sobre o baseline Windows-validado, com escopo restrito aos 3 arquivos da correção já provada (`graph_executor.py`, `test_fechamento_1_6_pid_reuse.py`, `tests/_helpers/pid_reuse_occupier.py`) e aos documentos. As evidências Windows de `HANDOFF_NOTES.md` e da seção 8 da auditoria foram preservadas.
