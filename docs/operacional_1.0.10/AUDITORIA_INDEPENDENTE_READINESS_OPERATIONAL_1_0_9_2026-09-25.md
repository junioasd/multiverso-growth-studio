# AUDITORIA INDEPENDENTE DE READINESS — v1.0.9
MULTIVERSO GROWTH STUDIO
Data: 2026-09-25

## VEREDITO

**NÃO APROVADA PARA READINESS AINDA.**

A v1.0.9 corrigiu, nos cenários focados, os quatro bloqueadores apontados na auditoria da v1.0.8. A nova auditoria adversarial independente encontrou, porém, **quatro bloqueadores adicionais**, concentrados em identidade/idempotência de reservas/uso e recuperação de moeda legada.

Nenhuma integração externa de mercado foi tratada como validada.

---

## PACOTE AUDITADO

`MULTIVERSO_GROWTH_STUDIO_OPERATIONAL_1.0.9_CANDIDATE.zip`

SHA-256 independente:

`05e6d7efa27174e0c1a5b2e49a7f557b71b725aaacd7799e4cef6f4576d33de4`

Integridade:

- ZIP: PASS (`testzip`: nenhum membro corrompido)
- entradas no ZIP: **544**
- inventário interno: **543/543 hashes corretos**
- ausentes: **0**
- divergentes: **0**
- `compileall`: **PASS**

---

## TESTES INDEPENDENTES REPRODUZIDOS

Executados diretamente no pacote recebido:

- v1.0.9 → v1.0.5: **84/84 PASS**
- v1.0.4 → v1.0.3: **27/27 PASS**
- v1.0.2: **13/13 PASS**
- v1.0.1 + correções da auditoria técnica + replay legado: **42/42 PASS**

Total focado independente:

**166/166 PASS — 0 FAIL — 0 ERROR**

O pacote declara regressão completa de 2.197 testes / 2.195 PASS / 0 FAIL / 0 ERROR / 2 SKIP. Esse total é evidência do construtor e **não é apresentado como reprodução independente integral** nesta auditoria.

---

## O QUE A v1.0.9 CORRIGIU DE VERDADE

Os testes focados independentes confirmaram, nos cenários cobertos:

1. existe fluxo público de recuperação para histórico em moeda legada não suportada;
2. retirada de moeda considera a moeda efetiva/materializada;
3. o uso sem custo grava snapshot da política vigente no evento;
4. resultados concorrentes com `reservation_id`/`correlation_id` distintos não cruzam reservas;
5. múltiplas reservas equivalentes sem identidade resultam em `AMBIGUOUS_RESERVATION_MATCH`.

Esses pontos estão corrigidos nos cenários testados.

---

# BLOQUEADOR 1 — `correlation_id` NÃO É VINCULADO AO PAYLOAD COMPLETO DA RESERVA

**Criticidade: CRÍTICA PARA LIMITES / IDEMPOTÊNCIA**
**Evidência: SQLITE REAL + API REAL DO COSTGUARD**

A v1.0.9 trata `correlation_id` como identidade idempotente, mas, ao encontrar uma reserva existente, compara somente:

- provider;
- service;
- operation.

Ela não compara:

- valor estimado/reservado;
- moeda;
- project;
- campaign;
- job_id;
- demais campos relevantes.

### Reprodução independente

1. limite total do provider: USD 100;
2. primeira reserva:
   - `estimated_cost = USD 1`
   - `correlation_id = cid`;
3. uma nova chamada fresca de USD 500 retorna corretamente:
   - `LIMIT_EXCEEDED`;
4. repetindo `correlation_id = cid`, mas agora com:
   - `estimated_cost = USD 500`;
   - project diferente;
5. o sistema retorna:

`AUTHORIZED`
`already_reserved = True`

usando a reserva antiga de USD 1.

### Impacto

Um retry divergente pode herdar uma reserva pequena e escapar da decisão atual de limite.

### Correção necessária

Persistir fingerprint completo da requisição/reserva e aplicar:

- mesmo correlation_id + mesmo payload = retry idempotente;
- mesmo correlation_id + payload divergente = `CORRELATION_ID_CONFLICT`;
- não considerar apenas provider/service/operation.

---

# BLOQUEADOR 2 — IDENTIDADE INFORMADA EM `record_usage()` NÃO É VALIDADA CONTRA A RESERVA

**Criticidade: ALTA/CRÍTICA PARA CORRELAÇÃO FINANCEIRA**
**Evidência: SQLITE REAL**

Ao receber `reservation_id`, `record_usage()` liquida a reserva, mas não verifica se os argumentos informados na entrega correspondem à reserva.

### Reprodução independente

Reserva persistida:

- provider `p`;
- service `s`;
- operation `o`.

Foi chamado:

- mesmo `reservation_id`;
- provider `p`;
- service `WRONG`;
- operation `WRONG`.

A chamada foi aceita e a reserva foi marcada `COMMITTED`.

O uso persistido ficou com os dados originais da reserva (`s/o`), enquanto os dados divergentes fornecidos pela entrega foram silenciosamente ignorados.

Em teste adicional, um `reservation_id` pertencente a outro provider também pode ser liquidado mesmo quando `record_usage()` é chamado com outro provider.

### Correção necessária

Antes de liquidar por `reservation_id` ou `correlation_id`, validar deterministamente os campos de identidade relevantes.

Divergência deve resultar em conflito explícito, sem alterar reserva, uso ou incidente.

---

# BLOQUEADOR 3 — USO SEM RESERVA NÃO POSSUI IDENTIDADE IDEMPOTENTE E UM RETRY PODE DUPLICAR A COBRANÇA

**Criticidade: CRÍTICA PARA CONTABILIDADE**
**Evidência: SQLITE REAL**

O fluxo de `record_usage()` sem reserva cria corretamente um `usage_id` e um incidente para custo desconhecido, mas não recebe uma identidade do evento que impeça duplicação.

### Reprodução independente

Provider com:

- limite total USD 1000;
- `unknown_cost_upper_bound = USD 100`.

A mesma chamada lógica foi registrada duas vezes:

`record_usage(provider='u', service='s', operation='o', success=True)`

Resultado:

- dois `usage_id` diferentes;
- dois incidentes `USAGE_COST_UNKNOWN`;
- ambos considerados eventos independentes.

Depois do write-off de ambos:

- uso 1 = USD 100;
- uso 2 = USD 100;
- total contabilizado = **USD 200**.

### Impacto

Retry de rede, redelivery ou repetição do mesmo evento sem reserva pode duplicar custo/incident.

### Correção necessária

Adicionar identidade idempotente para uso sem reserva:

- `usage_event_id` / `external_event_id` / `correlation_id`;
- índice único persistente;
- fingerprint dos campos relevantes;
- retry idêntico = mesmo `usage_id`;
- retry divergente = conflito.

Não depender apenas de provider/service/operation/timestamp.

---

# BLOQUEADOR 4 — MOEDA LEGADA PODE SER RETIRADA MESMO COM INCIDENTE ABERTO QUE A REFERENCIA NO SNAPSHOT, CRIANDO ESTADO SEM RECUPERAÇÃO

**Criticidade: CRÍTICA PARA LIVENESS FINANCEIRO**
**Evidência: SQLITE REAL**

Foi criado estado legado:

- limite `provider_total = 100 ZZZ`;
- `ZZZ` não é moeda suportada.

Depois foi registrado um uso sem custo.

O incidente criado ficou com:

- `currency = NULL`;
- `snapshot_currency = ZZZ`;
- status `OPEN`.

`retire_unsupported_currency_limits()` verifica incidentes pela coluna `currency`, mas não pelo `snapshot_currency`.

### Reprodução independente

1. uso desconhecido cria incidente aberto ligado ao evento;
2. `retire_unsupported_currency_limits()` remove o limite `ZZZ`;
3. o provider fica sem moeda operacional;
4. `reconcile_usage()` falha:
   - `moeda operacional ... indefinida`;
5. `reconcile_unsupported_currency_history()` também falha porque existe incidente aberto.

Resultado: o sistema cria um ciclo sem caminho normal de recuperação.

### Correção necessária

A retirada de moeda deve considerar todas as referências monetárias estruturadas do incidente, incluindo:

- `currency`;
- `snapshot_currency`;
- `original_currency`;
- `final_currency` quando aplicável;
- policy snapshot;
- moeda efetiva/materializada.

Não retirar a última configuração necessária para reconciliar um incidente aberto.

---

## VARIANTE ADICIONAL — RECONCILIAÇÃO NORMAL ACEITA MOEDA LEGADA NÃO SUPORTADA

**Criticidade: ALTA**
**Evidência: SQLITE REAL**

Com limite legado em `ZZZ` e incidente `USAGE_COST_UNKNOWN`, `valid_currency('ZZZ')` retorna `False`.

Mesmo assim:

`reconcile_usage(... final_amount=5, final_currency='ZZZ')`

foi aceito.

O lançamento terminou como:

- cost = 5;
- currency = ZZZ;
- budget_charge = 5;
- status = RECONCILED.

Isso contorna a política de moedas suportadas e o fluxo especial criado para moeda legada.

### Correção necessária

Reconciliação normal não deve criar novo estado financeiro em moeda não suportada.

Moeda legada não suportada deve passar somente pelo fluxo explícito de recuperação/reconciliação legado, com FX/evidência adequados.

---

## OBSERVAÇÃO DE LIFECYCLE DO `correlation_id`

Após uma reserva ser `COMMITTED`, uma nova chamada `authorize()` com o mesmo `correlation_id` ainda retorna:

`authorized = True`
`already_reserved = True`

Ela não informa que a operação já está terminal/concluída.

Isto deve ser revisto junto ao bloqueador 1 para que uma identidade concluída não seja apresentada como nova autorização operacional.

---

## DISTINÇÃO DE EVIDÊNCIA

Nesta auditoria foram realmente executados:

- código Python do pacote;
- SQLite real;
- CostGuard real;
- transações locais;
- testes de idempotência e reconciliação.

Continuam **NÃO VERIFICADOS EXTERNAMENTE**:

- YouTube;
- Meta/Facebook/Instagram;
- TikTok;
- Hotmart/Monetizze/Eduzz/Kiwify/Braip;
- gateways reais;
- providers pagos reais;
- Chromium/Playwright real;
- FX real;
- demais integrações externas.

Nenhuma dessas integrações é declarada funcional.

---

## DECISÃO

**NÃO avançar ainda para Baseline Day 0 nem piloto real.**

Próxima candidata recomendada:

`v1.0.10-OPERATIONAL-CANDIDATE`

Escopo focal:

1. fingerprint completo e lifecycle correto do `correlation_id`;
2. validar `record_usage()` contra a identidade real da reserva;
3. idempotência persistente para usos sem reserva;
4. preservar incidentes/snapshots de moeda legada durante retirement;
5. impedir reconciliação normal em moeda não suportada;
6. preservar todas as correções v1.0.1–v1.0.9;
7. nova auditoria independente antes da Baseline Day 0.
