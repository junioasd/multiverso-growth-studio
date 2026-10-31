# REPRODUÇÃO DA SUÍTE

## Requisitos
- Python 3.12 e as dependências de `requirements.txt` (`pip install -r requirements.txt`).
- Opcional: Chromium do Playwright, para os testes de navegador.

## Suíte completa (resultado de referência: 2.049 testes, 2.047 PASS, 2 SKIP)
```bash
python -m compileall -q .
python -W error::ResourceWarning -m unittest discover -s tests -t . -v
```

## Suítes por comando
```bash
python -W error::ResourceWarning -m unittest tests.test_fechamento_1_6_holder_liveness tests.test_fechamento_1_6_pid_reuse -v   # C1
python -W error::ResourceWarning -m unittest tests.test_comando_2_atlas_gemeo_digital -v                                       # C2
python -W error::ResourceWarning -m unittest tests.test_comando_3_navigator_forge_execucao tests.test_comando_3_navigator_planejamento_e2e -v  # C3
python -W error::ResourceWarning -m unittest tests.test_comando_4_radar_evolutivo_agentic -v                                   # C4
python -W error::ResourceWarning -m unittest tests.test_comando_5_maquina_comercial -v                                         # C5
python -W error::ResourceWarning -m unittest tests.test_comando_6_integracao_global -v                                         # C6
python -W error::ResourceWarning -m unittest tests.test_correcao_auditoria_independente -v                                    # correção pós-auditoria
python docs/evidencias/reproducao_bloqueadores_auditoria.py                                                                    # prova antes/depois
```

## Verificação estrita de vazamento de recursos
```bash
python tests/_helpers/strict_resource_runner.py tests.test_comando_6_integracao_global
```
- rc 0: limpo;
- rc 2: conexão SQLite vazada;
- rc 3: exceção ignorada em destrutor.

## Skips esperados (2)
`test_real_windows_*`: exigem Windows real. Foram executados e aprovados em Windows real no Comando 1 (36 PASS).

## Readiness em execução
`GET /api/health` (liveness) e `GET /api/health/ready` (readiness real; responde 503 quando UNHEALTHY).

## Correção final pós-reauditoria
```bash
python -W error::ResourceWarning -m unittest tests.test_correcao_reauditoria_migracao_replay -v
```
O upgrade real usa `docs/evidencias/reauditoria_etapa1_criar_banco_legado.py`, executado com o código do MASTER anterior, seguido de `reauditoria_etapa2_replay_pos_migracao.py`, executado com esta versão, sobre o mesmo banco.
