# AUDITORIA CIRÚRGICA FINAL DE READINESS — v1.0.11
MULTIVERSO GROWTH STUDIO  
Data: 2026-09-25

## VEREDITO

**NÃO APROVADA PARA READINESS AINDA — 1 BLOQUEADOR P1 REAL E REPRODUZÍVEL.**

Esta rodada permaneceu cirúrgica. Não foram procurados refinamentos teóricos. O objetivo foi exclusivamente verificar se a identidade/idempotência end-to-end corrigida na v1.0.11 realmente impede erros operacionais P0/P1 no caminho central de execução.

A v1.0.11 corrigiu o P1 da v1.0.10 nos cenários cobertos: `correlation_id` agora atravessa `run_metered_call`, retry idêntico concluído não reexecuta I/O, e `usage_event_id` impede que o mesmo evento liquide duas reservas.

Porém, a auditoria independente encontrou uma lacuna P1 dentro do próprio contrato de `correlation_id`: **o fingerprint não representa o payload operacional completo da chamada**.

---

## PACOTE AUDITADO

`MULTIVERSO_GROWTH_STUDIO_OPERATIONAL_1.0.11_CANDIDATE.zip`

SHA-256 independente:

`0564f0d57448e39654e0b6adff8527fd08c02707812b88adcaa2185ce3e0d75e`

Integridade independente:

- ZIP: PASS (`testzip`: nenhum membro corrompido)
- entradas: 558
- inventário interno: **557/557 hashes corretos**
- ausentes: 0
- divergentes: 0
- `compileall`: PASS

A suíte focal nova `tests.test_operacional_1_0_11` foi executada independentemente:

**14/14 PASS — 0 FAIL — 0 ERROR**

A regressão global registrada no pacote é:

- 2.229 testes
- 2.227 PASS
- 0 FAIL
- 0 ERROR
- 2 SKIP

Esse total global é evidência do construtor; não é apresentado como reprodução independente integral nesta rodada.

---

## O QUE A v1.0.11 CORRIGIU DE VERDADE

Foram confirmados no código e nos testes focados:

1. `run_metered_call()` repassa `correlation_id` à reserva.
2. retry idêntico de uma chamada já concluída retorna `ALREADY_COMPLETED` e não executa `perform()` novamente.
3. `usage_event_id` é consultado antes do matching heurístico.
4. o mesmo `usage_event_id` não liquida uma segunda reserva criada posteriormente.
5. o índice único de `usage_event_id` protege redelivery concorrente.

Esses pontos estão corrigidos nos cenários cobertos.

---

# BLOQUEADOR P1 — `correlation_id` NÃO ESTÁ VINCULADO AO PAYLOAD OPERACIONAL COMPLETO

## Evidência

O fingerprint persistido pela reserva é construído a partir de:

- provider
- service
- operation
- estimated_cost
- currency
- project
- campaign
- job_id

Porém, `run_metered_call()` recebe um dicionário `params` que contém os dados reais da operação externa — por exemplo:

- prompt
- query
- conteúdo
- parâmetros de geração
- opções do provider
- demais argumentos que modificam a execução

Esses campos **não entram no fingerprint**.

### Reprodução independente 1 — caminho central

Primeira chamada:

- `correlation_id = "cid-1"`
- `estimated_cost = 1`
- `currency = USD`
- `prompt = "PROMPT A"`

Segunda chamada:

- mesmo `correlation_id`
- mesmo custo/moeda/metadados
- `prompt = "PROMPT B"`

Resultado observado:

- primeira chamada executou normalmente;
- segunda chamada **não gerou `CORRELATION_ID_CONFLICT`**;
- foi tratada como `ALREADY_COMPLETED`;
- o fingerprint persistido não continha `PROMPT A` nem `PROMPT B`.

Fingerprint observado:

`["reservation_payload", "paid", "gen", "gen", "1.0", "USD", null, null, null]`

### Reprodução independente 2 — HTTP loopback real

Foi executado o caminho:

`HttpGenerationProvider → run_metered_call → HTTP socket loopback`

Primeira chamada:

- correlation_id `same-id`
- prompt `A`

Segunda chamada:

- correlation_id `same-id`
- prompt `B`

O servidor HTTP recebeu apenas uma requisição, contendo o prompt A.

A segunda chamada retornou:

`ALREADY_COMPLETED`

e não:

`CORRELATION_ID_CONFLICT`.

Isso confirma que duas operações semanticamente diferentes podem ser consideradas o mesmo retry apenas porque seus metadados financeiros coincidem.

---

## IMPACTO

Esse comportamento quebra a regra de idempotência:

**mesma identidade + mesmo payload = retry idempotente**

**mesma identidade + payload diferente = conflito**

Na v1.0.11, uma mudança em campos que alteram a operação externa pode passar despercebida.

Consequências possíveis:

- uma nova geração/pesquisa diferente pode ser suprimida como se fosse retry;
- o sistema pode devolver estado anterior para uma requisição semanticamente distinta;
- a trilha de auditoria pode associar uma identidade a uma intenção diferente da originalmente executada;
- bugs de integração que reutilizem um `correlation_id` não serão detectados se custo/metadados permanecerem iguais.

É P1 porque ocorre no caminho central que controla I/O pago e identidade operacional.

---

## CAUSA-RAIZ

O fingerprint foi implementado em `reserve()`, que recebe apenas metadados da reserva.

`run_metered_call()` possui o payload operacional completo em `params`, mas não o vincula ao fingerprint da identidade.

O comentário do código afirma "payload completo", porém o fingerprint persistido cobre somente o payload financeiro/organizacional da reserva.

---

## CORREÇÃO CIRÚRGICA RECOMENDADA

Gerar somente:

`v1.0.12-OPERATIONAL-CANDIDATE`

Sem nova arquitetura.

### Requisito

`correlation_id` precisa ser vinculado a um fingerprint determinístico do **payload operacional completo relevante** antes do I/O.

Uma abordagem segura:

1. `run_metered_call()` calcula um `request_fingerprint` canônico a partir de:
   - provider;
   - operation;
   - parâmetros operacionais que serão enviados ao provider;
   - metadados financeiros/escopo relevantes;
2. excluir apenas campos explicitamente não semânticos, se houver, documentando-os;
3. passar esse fingerprint para `reserve()`;
4. persistir junto à reserva;
5. retry:
   - fingerprint igual → idempotente;
   - fingerprint diferente → `CORRELATION_ID_CONFLICT` antes do I/O;
6. usar serialização canônica, não `repr(dict)` dependente de ordem/representação.

### Testes mínimos

- mesmo correlation_id + mesmo prompt → retry idempotente;
- mesmo correlation_id + prompt diferente → conflito;
- mesmo correlation_id + query diferente → conflito;
- ordem diferente das chaves do mesmo JSON → mesmo fingerprint;
- estruturas aninhadas equivalentes → mesmo fingerprint;
- parâmetro operacional diferente → conflito antes de I/O;
- concorrência;
- restart;
- regressão v1.0.11.

---

## DISTINÇÃO DE EVIDÊNCIA

**EXECUTADO REALMENTE nesta auditoria:**

- bytes do ZIP;
- verificação SHA-256;
- integridade ZIP;
- inventário SHA-256;
- `compileall`;
- suíte v1.0.11;
- SQLite real;
- caminho `run_metered_call`;
- HTTP socket loopback real.

**NÃO VERIFICADO EXTERNAMENTE:**

- provedores pagos reais;
- YouTube;
- Meta/Facebook/Instagram;
- TikTok;
- Hotmart/Monetizze/etc.;
- gateways reais;
- Chromium/Playwright real;
- FX real;
- demais integrações externas.

---

## DECISÃO

**NÃO avançar ainda para Baseline Day 0.**

Existe apenas **um P1 focal** identificado nesta auditoria final: o fingerprint do `correlation_id` não cobre o payload operacional completo.

Recomendação: fazer uma microcorreção v1.0.12 exclusivamente nesse ponto, executar uma última verificação cirúrgica e, se nenhum P0/P1 real permanecer, congelar a versão operacional e avançar para o Day 0.
