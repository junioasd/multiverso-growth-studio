# AUDITORIA CIRÚRGICA DE READINESS — v1.0.10
MULTIVERSO GROWTH STUDIO  
Data: 2026-09-25

## VEREDITO

**NÃO APROVADA PARA READINESS AINDA — 1 BLOQUEADOR P1 REAL E REPRODUZÍVEL.**

Esta rodada foi deliberadamente cirúrgica. O objetivo não foi procurar refinamentos teóricos, mas somente falhas P0/P1 com impacto operacional concreto antes do Baseline Day 0.

A v1.0.10 corrigiu os bloqueadores focais da v1.0.9 nos cenários testados. Porém, a identidade/idempotência recém-implementada ainda **não está conectada de ponta a ponta aos caminhos reais de execução**.

---

## PACOTE AUDITADO

`MULTIVERSO_GROWTH_STUDIO_OPERATIONAL_1.0.10_CANDIDATE.zip`

SHA-256 independente:

`f78ee1975e2d7f99013ff78312e199230971c5a6875879c0f0e9ad39c88a16ec`

Integridade independente:

- ZIP: PASS (`testzip`: nenhum membro corrompido)
- 551 entradas
- inventário interno: **550/550 hashes corretos**
- 0 ausentes
- 0 divergentes
- `compileall`: PASS

---

## TESTES INDEPENDENTES FOCADOS

Foram executadas diretamente no pacote as suítes:

- `tests.test_operacional_1_0_10`
- `tests.test_operacional_1_0_9`
- `tests.test_operacional_1_0_8`
- `tests.test_operacional_1_0_7`
- `tests.test_operacional_1_0_6`
- `tests.test_operacional_1_0_5`
- `tests.test_operacional_1_0_4`
- `tests.test_operacional_1_0_3`
- `tests.test_operacional_1_0_2`
- `tests.test_operacional_1_0_1`
- `tests.test_correcao_auditoria_independente`
- `tests.test_correcao_reauditoria_migracao_replay`

Total independente focado:

**184/184 PASS — 0 FAIL — 0 ERROR**

O pacote declara regressão completa de 2.215 testes / 2.213 PASS / 0 FAIL / 0 ERROR / 2 SKIP. Esse total global é evidência do construtor; não é apresentado aqui como reprodução independente integral.

---

# BLOQUEADOR P1 — IDENTIDADE/IDEMPOTÊNCIA NÃO ESTÁ LIGADA DE PONTA A PONTA AO CAMINHO REAL DE EXECUÇÃO

## Parte A — `run_metered_call()` ignora `correlation_id`

**Evidência: execução real local do caminho central de provider pago + SQLite real**

`run_metered_call()` é documentado no próprio código como o caminho único para chamada cobrável.

A v1.0.10 adicionou proteção forte de `correlation_id` em `reserve()/authorize()`, mas `run_metered_call()` não repassa `params["correlation_id"]` para `guard.reserve()`.

### Reprodução independente

Configuração:

- provider pago com limite USD 100;
- mesma chamada lógica executada duas vezes;
- `params` continha `correlation_id = "logical-1"`;
- `perform()` incrementava um contador observável.

Resultado:

- `perform()` foi chamado **2 vezes**;
- foram criadas **2 reservas distintas**;
- ambas tinham `correlation_id = NULL`;
- foram criados **2 lançamentos de uso**;
- custo total contabilizado: **USD 2**.

Portanto, o mecanismo de idempotência existe, mas o caminho central que realmente faz o I/O pago não o utiliza.

### Impacto

Retry/reentrega/reexecução da mesma chamada lógica pode:

- executar I/O externo novamente;
- consumir API paga novamente;
- duplicar custo;
- duplicar uma ação externa não idempotente.

Isso é bloqueador de readiness porque ocorre no caminho central de execução, não em função auxiliar isolada.

---

## Parte B — `record_usage()` ignora `usage_event_id` quando encontra exatamente uma reserva aberta

**Evidência: SQLite real + API real do CostGuard**

A v1.0.10 introduziu `usage_event_id` idempotente para uso sem reserva.

Porém a resolução heurística de reserva acontece **antes** da lógica de `usage_event_id`.

Se existir exatamente uma reserva aberta equivalente, `record_usage()` associa o evento a essa reserva e não persiste/verifica o `usage_event_id`.

### Reprodução independente

1. Reserva A criada.
2. Evento `evt-dup` entregue sem `reservation_id/correlation_id`, mas com `usage_event_id="evt-dup"`.
3. Como A era o único candidato, A foi liquidada.
4. Reserva B foi criada depois, com a mesma identidade semântica.
5. O mesmo evento `evt-dup` foi entregue novamente.
6. Como B era agora o único candidato, o mesmo evento liquidou B.

Resultado:

- A = `COMMITTED`;
- B = `COMMITTED`;
- dois lançamentos financeiros;
- `usage_event_id` não apareceu nos lançamentos ligados às reservas;
- nenhum conflito/idempotência foi acionado.

### Impacto

Um retry atrasado ou redelivery pode ser confundido com uma operação posterior e liquidar a reserva errada, mesmo quando o evento traz um `usage_event_id`.

---

# CAUSA-RAIZ

A identidade está implementada em componentes isolados, mas ainda não forma um **contrato end-to-end**.

Atualmente existem três identidades:

- `correlation_id` da reserva;
- `reservation_id`;
- `usage_event_id` da entrega.

Mas os caminhos de execução não garantem que uma mesma identidade viaje da origem → reserva → I/O → entrega → settlement.

---

# CORREÇÃO MÍNIMA RECOMENDADA

Próxima candidata:

`v1.0.11-OPERATIONAL-CANDIDATE`

Escopo estritamente focal:

1. `run_metered_call()` deve aceitar/repassar `correlation_id` ao `reserve()`.
2. Retry idêntico já concluído **não pode executar `perform()` novamente**.
3. Quando houver reserva, a entrega deve exigir `reservation_id` ou `correlation_id`; não usar “único candidato atual” como identidade de evento.
4. `usage_event_id`, quando fornecido, deve ser persistido/verificado também nos settlements ligados a reservas, ou mapeado atomicamente a uma única reserva/entrega.
5. Mesmo `usage_event_id` nunca pode liquidar duas reservas.
6. Testar:
   - retry do mesmo `run_metered_call`;
   - retry depois de `COMMITTED`;
   - redelivery atrasado depois da criação de uma nova reserva;
   - concorrência;
   - restart;
   - replay divergente.

Não é necessário abrir nova frente arquitetural.

---

## O QUE NÃO FOI VALIDADO

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

---

## DECISÃO

**NÃO avançar ainda para Baseline Day 0.**

Esta auditoria encontrou **um bloqueador P1 de identidade/idempotência end-to-end**. A recomendação é uma única correção focal v1.0.11, seguida de uma última auditoria cirúrgica deste caminho antes do congelamento operacional.
