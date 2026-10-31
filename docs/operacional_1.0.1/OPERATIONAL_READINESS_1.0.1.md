# OPERATIONAL READINESS 1.0.1 — MULTIVERSO GROWTH STUDIO

**Versão:** `1.0.1-OPERATIONAL-CANDIDATE` (status CANDIDATE, aguardando auditoria). **Baseline:** v1.0.0 `932292a9…e42fe`, inalterada.
**Ambiente:** sandbox local (Linux, Python 3.12); comandos reais: main.py backup/worker/scheduler/readiness; nenhuma integração externa. **Gerado em:** 2026-09-25T00:51:37Z (UTC).

## Sinalizadores
| Sinalizador | Valor |
|---|---|
| TECHNICALLY_READY | True |
| OPERATIONALLY_READY | False |
| PUBLIC_EXPOSURE_READY | False |
| EXTERNAL_INTEGRATION_PENDING | True |

`TECHNICALLY_READY` = o sistema funciona. `OPERATIONALLY_READY` = falso **apenas** por configuração do Owner (segredos, credencial, TLS). `EXTERNAL_INTEGRATION_PENDING` = credenciais de integração ausentes — **não é bug interno**.

## Verificações operacionais (execução real dos comandos)
| Verificação | Status | Detalhe |
|---|---|---|
| kill_switch | `READY` | estado=DISABLED |
| worker | `READY` | último heartbeat há 0.6s |
| scheduler | `READY` | última janela há 0.3s |
| cost_guard | `READY` | ativo; provedores com limite: nenhum (sem limite => PAID_PROVIDER_NOT_AUTHORIZED) |
| logging | `READY` | LOG_DIR=/home/claude/opdata101/logs |
| backup | `READY` | último backup 20260925T005046809647Z (LOCAL_BACKUP_ONLY) |
| db_path | `READY` | DB_PATH explícito fora de local temporário e fora do código (volume não auditável daqui) |
| vault_master_key | `REQUIRES_OWNER_CONFIGURATION` | MGS_VAULT_MASTER_KEY ausente |
| session_secret | `REQUIRES_OWNER_CONFIGURATION` | SESSION_SECRET ausente |
| owner_credential | `REQUIRES_OWNER_CONFIGURATION` | OWNER_EMAIL/OWNER_PASSWORD_HASH ausentes — usar `main.py set-owner-password` |
| tls | `PUBLIC_EXPOSURE_NOT_READY` | sem proxy TLS declarado — não expor publicamente |

## Bloqueadores internos restantes
**Nenhum.** Todos os bloqueadores internos do Day 0 foram implementados e testados.

## Itens exclusivamente do Owner
- **vault_master_key**: gerar localmente e definir MGS_VAULT_MASTER_KEY (≥32 caracteres aleatórios) como variável do sistema/cofre — nunca em arquivo versionado nem em chat
- **session_secret**: gerar e definir SESSION_SECRET (≥32 caracteres aleatórios) da mesma forma
- **owner_credential**: definir OWNER_EMAIL e gerar OWNER_PASSWORD_HASH com `python main.py set-owner-password` (senha digitada sem eco)
- **tls**: colocar a aplicação atrás de proxy TLS real (certificado válido), definir BEHIND_TLS_PROXY=1 e APP_ENV=production
- **DB_PATH em volume persistente de produção (este teste usou diretório local do sandbox; volume real não é auditável daqui)**: REQUIRES_OWNER_CONFIGURATION
- **destino EXTERNO de backup (hoje LOCAL_BACKUP_ONLY) — BACKUP_EXTERNAL_TARGET + processo de cópia**: REQUIRES_OWNER_CONFIGURATION
- **limites de custo por provedor pago (sem limite => PAID_PROVIDER_NOT_AUTHORIZED por padrão)**: REQUIRES_OWNER_CONFIGURATION
- **executar worker/scheduler/backup sob supervisor (systemd/serviço Windows/Agendador) com LOG_DIR/BACKUP_DIR definidos**: REQUIRES_OWNER_CONFIGURATION

## Itens exclusivamente de serviços externos
- YouTube Data API v3 (OAuth2)
- Meta Graph API (Facebook/Instagram)
- TikTok API
- Hotmart/Monetizze/Eduzz/Kiwify/Braip
- Gateway de pagamento
- Analytics
- Provedores de vídeo IA (Seedance/Kling/Veo/Sora)
- LLM pago
- Provedores de pesquisa dos agentes
- Modelo de visão
- Acesso web (egress) do RADAR
- Chromium do Playwright

## Comparação com o Day 0
- **kill_switch**: NOT_READY → READY
- **workers**: NOT_CONFIGURED → READY (comando explícito)
- **scheduler**: NOT_CONFIGURED → READY (comando explícito)
- **logs**: NOT_CONFIGURED → READY (com LOG_DIR)
- **custo_ia_api**: NOT_READY → READY (bloqueio padrão sem limite)
- **backup**: NOT_CONFIGURED → READY (LOCAL_BACKUP_ONLY)
- **db_path**: NOT_CONFIGURED → validação implementada; volume real: Owner
- **secrets**: NOT_CONFIGURED → validação implementada; valores: Owner
- **https_cookie**: NOT_CONFIGURED → cookie Secure por ambiente; TLS real: Owner
