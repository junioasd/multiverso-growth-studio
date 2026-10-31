# REAUDITORIA INDEPENDENTE — MULTIVERSO GROWTH STUDIO

Data: 2026-09-24  
Pacote auditado: `MULTIVERSO_GROWTH_STUDIO_MASTER_FINAL_CORRIGIDO_2026-09-24.zip`

## Veredito

**NÃO APROVADO**

Os quatro bloqueadores da auditoria anterior foram corrigidos e reproduzidos com sucesso em testes independentes. Porém, a reauditoria encontrou um novo bloqueador de integridade no caminho de migração de bancos existentes: eventos processados antes da introdução de `event_fingerprints_json` são migrados com mapa de fingerprints vazio e um replay divergente do mesmo `gateway_event_id` continua sendo aceito silenciosamente como retry.

## 1. Integridade do pacote

- SHA-256 verificado: `6203d55398166d1b6d26ff74a1e674e7edbddb64b51757a1c19d1d91b7b6721b`
- `unzip -t`: OK, nenhum erro nos dados compactados.
- Inventário interno `docs/INVENTARIO_SHA256.txt`: 461 arquivos verificados, 0 divergências.
- `python3 -m compileall -q .`: PASS.

## 2. Regressão completa independente

Execução:

```bash
python3 -W error::ResourceWarning -m unittest discover -s tests -v
```

Resultado:

- 2.040 testes executados
- 2.033 PASS
- 0 FAIL
- 0 ERROR
- 7 SKIP
- RC = 0
- duração observada: 207,685 s

Os skips são dependentes do ambiente/plataforma e não produziram falha da suíte.

## 3. Repetição independente dos quatro bloqueadores anteriores

### A — MOCK_FALLBACK promovido a VERIFIED

**PASS.**

Resultado observado:
- evidência `mode=MOCK`;
- `verified=False`;
- `trace.verified=False`;
- missão final `FAILED`, não `VERIFIED`;
- erro `SIMULATED_NOT_VERIFIED`;
- apenas 1 tentativa, sem reexecução da simulação.

### B — navegador confundindo navegação com publicação

**PASS.**

Ataque executado com `PUBLISH_POST`, aprovação Owner válida e navegador de prova que registraria qualquer visita.

Resultado:
- `success=False`;
- `delivery_state=NOT_SENT`;
- navegador não foi acionado;
- nenhuma navegação foi usada como prova de mutação.

### C — replay divergente de pagamento

**PASS para eventos criados já na versão corrigida.**

Após pagamento de 100 com `gateway_event_id=g1`:
- replay do mesmo ID com valor 1 => `JourneyConflict`;
- replay do mesmo ID com valor 999 => `JourneyConflict`;
- replay do mesmo ID e mesmo valor com evidência diferente => `JourneyConflict`;
- replay idêntico => aceito de forma idempotente;
- somente uma receita registrada.

### D — reutilização divergente de order_id

**PASS.**

Após criação de `order_id=o2`, tentativas com mesmo ID e canal/valor/oferta/campanha diferentes foram rejeitadas com `JourneyConflict`. Repetição do payload idêntico permaneceu idempotente.

## 4. Novo bloqueador encontrado — migração de eventos legados sem fingerprint

### Classe

Integridade / anti-replay / migração de estado persistente.

### Causa

A migração adiciona:

`event_fingerprints_json TEXT NOT NULL DEFAULT '{}'`

para pedidos existentes. Em `commercial/journey.py`, `_check_replay()` somente compara conteúdo quando já existe fingerprint armazenado. Para evento legado presente em `processed_events_json` mas ausente de `event_fingerprints_json`, a função aceita o replay como idempotente sem poder provar que o conteúdo é o mesmo.

O próprio código documenta atualmente: `Linha legada sem fingerprint registrado: retry idempotente.`

### Reprodução real de upgrade

A prova foi feita em duas etapas, sem fabricar um banco apenas com a versão corrigida:

1. O MASTER FINAL anterior (`9b498594...`) criou um banco e processou um pagamento real interno:
   - pedido `legacy_real`;
   - valor 100;
   - `gateway_event_id=g_old`;
   - status final `PAID`;
   - o schema antigo não possuía `event_fingerprints_json`.

2. O mesmo arquivo SQLite foi aberto pelo MASTER FINAL CORRIGIDO. A migração criou a coluna com `{}`.

Estado após a migração:

```text
processed_events_json = ["g_old"]
event_fingerprints_json = {}
```

3. Foram enviados replays divergentes do evento já processado:

```text
same gateway_event_id = g_old
paid_amount = 1.0
evidence = tampered
```

Resultado: **aceito**, retornando `PAID`.

Outro replay:

```text
same gateway_event_id = g_old
paid_amount = 999.0
evidence = gateway-original
```

Resultado: **aceito**, retornando `PAID`.

### Por que é bloqueador

O requisito pós-auditoria era: mesmo ID + mesmo conteúdo pode ser retry idempotente; mesmo ID + conteúdo diferente deve gerar conflito/rejeição.

No caminho de upgrade de estado persistente, essa garantia não é verdadeira. O sistema não altera receita nem duplica cobrança nesse replay, mas aceita silenciosamente conteúdo divergente e não consegue provar integridade do evento. Isso viola o contrato de anti-replay e a exigência de persistência/migração segura dos Comandos Mestres.

## 5. Correção exigida

Para eventos legados que aparecem em `processed_events_json` sem fingerprint correspondente, o sistema deve **falhar fechado**. Não é seguro assumir equivalência do payload que não pode ser comprovada.

Uma solução aceitável deve garantir, no mínimo:

- evento novo: mesmo ID + mesmo fingerprint => retry idempotente;
- evento novo: mesmo ID + fingerprint diferente => `JourneyConflict`;
- evento legado processado sem fingerprint verificável => não aceitar silenciosamente payload reapresentado como idêntico;
- definir comportamento explícito e seguro para legado, por exemplo `JourneyConflict` / `LEGACY_EVENT_UNVERIFIABLE`, ou migração/backfill somente quando houver evidência histórica suficiente para reconstruir fingerprint;
- preservar compatibilidade de leitura de pedidos antigos;
- teste obrigatório que crie um banco no schema anterior, processe evento, rode migração e tente replay divergente após upgrade;
- teste de restart e concorrência após migração;
- regressão completa novamente.

**Não fabricar fingerprint a partir do payload do replay recebido**, pois isso permitiria ao primeiro replay pós-upgrade definir retroativamente qual era o conteúdo original.

## 6. Mudanças do pacote corrigido

Comparação com o MASTER anterior:

- 455 arquivos no antigo;
- 462 no corrigido;
- 23 caminhos adicionados/alterados;
- produção alterada em 6 arquivos:
  - `atlas/director.py`
  - `bridge/execution/navigator_real.py`
  - `commercial/journey.py`
  - `core/models/db.py`
  - `core/reasoning/adapters.py`
  - `core/reasoning/engine.py`

Os quatro defeitos anteriores estão de fato corrigidos nesses caminhos.

## 7. Validação externa

Integrações reais de plataformas, credenciais, anúncios, gateways externos, browser real gerenciado e resultados de mercado continuam dependendo de validação externa/operação real. Isso permanece separado desta reprovação.

## Conclusão

**NÃO APROVADO.**

A versão corrigida melhorou substancialmente e fechou os quatro bloqueadores anteriores, mas ainda não satisfaz integralmente a propriedade anti-replay em bancos migrados do MASTER anterior. Corrigir esse único bloqueador, gerar novo MASTER FINAL e repetir a reauditoria.
