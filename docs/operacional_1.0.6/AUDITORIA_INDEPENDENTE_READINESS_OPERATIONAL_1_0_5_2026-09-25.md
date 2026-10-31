# AUDITORIA INDEPENDENTE DE READINESS — v1.0.5
MULTIVERSO GROWTH STUDIO  
Data: 2026-09-25

## VEREDITO

**NÃO APROVADA PARA READINESS AINDA.**

A v1.0.5 corrigiu de forma reproduzível os quatro bloqueadores da auditoria da v1.0.4 e a observação estrutural do navegador. Porém, uma nova rodada adversarial independente encontrou **quatro bloqueadores de integridade operacional/financeira** que precisam ser fechados antes de Baseline Day 0 e antes de qualquer piloto real.

Nenhuma integração externa de mercado foi tratada como validada nesta auditoria.

---

## 1. PACOTE AUDITADO

O upload recebido foi um pacote externo `files (14).zip` contendo o candidato e os arquivos de evidência.

Candidato interno auditado:

`MULTIVERSO_GROWTH_STUDIO_OPERATIONAL_1.0.5_CANDIDATE.zip`

SHA-256 independente:

`0b1476fbc22cbd5942da6f19817495d3ce3127fb84ab41fbe6fa084625c32e00`

O hash coincide com `SHA256SUMS.txt` e com o `RELEASE_MANIFEST.json`.

Integridade:

- ZIP interno: **PASS**
- `testzip`: nenhum membro corrompido
- entradas: **516**
- inventário SHA-256 interno: **515/515 arquivos conferidos**
- ausentes: **0**
- divergentes: **0**
- `compileall`: **PASS**

---

## 2. TESTES INDEPENDENTES EXECUTADOS

Foram executadas independentemente:

- `tests.test_operacional_1_0_5`: **13/13 PASS**
- `tests.test_operacional_1_0_4` + `1_0_3` + `1_0_2`: **40/40 PASS**
- `tests.test_operacional_1_0_1`: **24/24 PASS**
- correções da auditoria técnica + replay legado: **18/18 PASS**

Total focado independente:

**95/95 PASS — 0 FAIL — 0 ERROR**

O pacote contém uma regressão global registrada pelo construtor:

- 2.126 testes
- 2.124 PASS
- 0 FAIL
- 0 ERROR
- 2 SKIP
- RC=0

Esse total global é **evidência do pacote**, não é apresentado aqui como reprodução independente integral. A presente auditoria reproduziu os 95 testes focados acima e executou ataques adversariais adicionais.

---

## 3. O QUE A v1.0.5 CORRIGIU DE VERDADE

Os testes independentes confirmaram:

1. `api/server.py::run_server()` ativa a camada operacional quando constrói a própria aplicação.
2. estimativa zero em provider pago é rejeitada.
3. custo desconhecido sem upper bound confiável abre incidente e bloqueia novas chamadas.
4. incidentes financeiros não podem mais ser encerrados apenas mudando status.
5. divergência de settlement da mesma `reservation_id` gera `SettlementConflict`.
6. settlement legado sem fingerprint falha fechado.
7. a checagem do kill switch ocorre novamente antes de `page.evaluate()` no caminho testado estruturalmente.

Essas correções são reais nos cenários cobertos.

---

# BLOQUEADOR 1 — `unknown_cost_upper_bound = 0` É ACEITO COMO “TETO CONFIÁVEL”

**Criticidade: CRÍTICA**  
**Evidência: EXECUÇÃO HTTP LOCAL REAL + SQLITE REAL**

A nova política depende de `unknown_cost_upper_bound` para permitir uma chamada paga cujo custo real possa não ser informado.

Porém `set_limit()` aceita valor `0` para esse tipo de limite.

### Reprodução independente

Configuração:

- `provider_total = USD 10`
- `unknown_cost_upper_bound = USD 0`
- `estimated_cost = USD 1`

Foi usado `HttpGenerationProvider` contra um servidor HTTP loopback real controlado pela auditoria. O servidor devolveu sucesso sem campo `cost`.

Foram feitas duas chamadas.

Resultado observado:

- servidor recebeu **2 requests reais** (`/gen`, `/gen`);
- ambas as chamadas retornaram `available=True`;
- **nenhum incidente** foi aberto;
- ledger:
  - `calls = 2`
  - `calls_cost_unknown = 2`
  - `budget_charged = 2.0`

Ou seja: um upper bound igual a zero, que não pode representar um teto positivo confiável para uma chamada paga, é suficiente para suprimir `COST_UNKNOWN_NO_UPPER_BOUND`.

### Correção necessária

Para `kind = unknown_cost_upper_bound`:

- exigir valor **estritamente > 0**;
- validar o valor no `set_limit()` e também no uso;
- upper bound zero/ausente/inválido deve ser tratado como **sem upper bound confiável**;
- custo real desconhecido nesse cenário deve abrir incidente e bloquear o provider.

---

# BLOQUEADOR 2 — DUAS RECONCILIAÇÕES CONCORRENTES DO MESMO INCIDENTE PODEM SER ACEITAS

**Criticidade: CRÍTICA**  
**Evidência: SQLITE REAL + CONCORRÊNCIA REAL EM THREADS**

`reconcile_incident()` lê o incidente aberto, altera o ledger e só depois grava a resolução, sem uma transação única/CAS que garanta que apenas uma reconciliação vence.

### Reprodução independente

Foi criado um `CURRENCY_MISMATCH_INCIDENT`.

Duas threads reconciliaram simultaneamente o **mesmo `incident_id`**:

- reconciliação A: USD 10
- reconciliação B: USD 20

Resultado observado:

- **as duas chamadas retornaram sucesso**;
- nenhuma recebeu conflito;
- o valor final persistido dependeu da última gravação;
- no teste reproduzido, o incidente/ledger terminaram em USD 20.

Em outra execução durante a auditoria, a ordem final foi a inversa — evidência de condição de corrida.

### Impacto

Uma repetição concorrente, retry de UI/API ou duas sessões Owner podem sobrescrever silenciosamente uma reconciliação financeira já concluída.

### Correção necessária

`reconcile_incident()` deve ser atomicamente idempotente:

- `BEGIN IMMEDIATE`/transação equivalente;
- ler e reclamar o incidente na mesma transação;
- update final com condição `status='OPEN'`;
- exatamente um reconciliador pode concluir;
- retry com conteúdo idêntico = idempotente;
- retry com conteúdo divergente = conflito explícito;
- ledger e status do incidente devem ser commitados juntos.

---

# BLOQUEADOR 3 — TROCA DA MOEDA DO LIMITE MISTURA HISTÓRICO DE OUTRA MOEDA SEM FX

**Criticidade: CRÍTICA**  
**Evidência: SQLITE REAL**

`_committed()` soma `budget_charge` por provider sem filtrar a moeda.

Ao mesmo tempo, `set_limit()` permite substituir a moeda do limite existente.

### Reprodução independente

1. limite do provider: `USD 100`
2. gasto confirmado: `USD 50`
3. Owner altera o mesmo `provider_total` para `EUR 100`
4. preview de nova chamada:
   - EUR 40 = autorizada
   - EUR 60 = bloqueada porque o sistema calcula:
     `gasto 50 + 60 > 100 EUR`

O problema: os `50` usados no cálculo são **USD 50 históricos**, tratados numericamente como **EUR 50**, sem conversão e sem incidente.

O próprio summary continua mostrando o gasto como `fxp/USD = 50`, confirmando que o histórico não foi convertido.

### Impacto

O orçamento pode ficar incorreto após mudança de moeda. Dependendo das moedas envolvidas, a distorção pode superestimar ou subestimar o gasto.

### Correção necessária

Uma destas políticas deve ser adotada explicitamente:

- impedir mudança de moeda enquanto houver histórico/reservas/incidentes do provider; ou
- exigir migração/reconciliação de moeda com FX real, fonte e timestamp antes da troca.

Nunca somar valores de moedas diferentes como se fossem equivalentes.

---

# BLOQUEADOR 4 — ESTADOS DE SETTLEMENT INCONSISTENTES PODEM LIBERAR OU SUBCONTABILIZAR CUSTO REAL

**Criticidade: ALTA/CRÍTICA PARA INTEGRIDADE FINANCEIRA**  
**Evidência: SQLITE REAL / API CENTRAL DO COSTGUARD**

O método central `settle()` aceita combinações semanticamente contraditórias sem incidente.

### Reprodução A

Reserva:

- USD 1

Settlement:

- `outcome = NOT_EXECUTED`
- `actual_cost = USD 100`
- `cost_confirmed = True`

Resultado:

- reserva = `RELEASED`
- `budget_charge = 0`
- nenhum incidente
- próxima reserva continua autorizada

O custo real informado foi ignorado porque `NOT_EXECUTED` domina a lógica.

### Reprodução B

Reserva:

- USD 1
- upper bound confiável configurado em USD 1

Settlement:

- `outcome = UNKNOWN`
- `actual_cost = USD 100`
- `cost_confirmed = True`

Resultado:

- `HELD_UNKNOWN`
- `budget_charge = 1`
- nenhum incidente
- o custo real informado de USD 100 não é contabilizado

### Correção necessária

Definir invariantes explícitos:

- `NOT_EXECUTED + actual_cost > 0` não pode liberar orçamento silenciosamente;
- `UNKNOWN + actual_cost conhecido` deve preservar/registrar o custo conhecido ou gerar incidente de inconsistência;
- combinações contraditórias devem gerar `SETTLEMENT_STATE_CONFLICT` ou equivalente;
- nenhum valor real informado pode ser descartado em favor de um estado textual contraditório.

---

## 4. OBSERVAÇÕES NÃO BLOQUEADORAS / PENDÊNCIAS EXTERNAS

### Chromium/Playwright

A correção de `page.evaluate()` passou no teste estrutural da suíte v1.0.5.

**Não foi validada com Chromium real nesta auditoria.**

Portanto continua:

`PENDENTE DE VALIDAÇÃO EXTERNA/AMBIENTE`

### Integrações reais

Não foram validadas nesta auditoria:

- YouTube
- Meta/Facebook/Instagram
- TikTok
- Hotmart/Monetizze/Eduzz/Kiwify/Braip
- gateway real
- analytics externo
- providers reais de vídeo
- LLM pago
- pesquisa externa real
- modelo de visão
- demais serviços externos

Nenhum desses itens é declarado funcional com base apenas em testes locais.

---

## 5. DECISÃO

**NÃO avançar ainda para Baseline Day 0 nem piloto real.**

Próxima candidata recomendada:

`v1.0.6-OPERATIONAL-CANDIDATE`

Escopo focal:

1. upper bound desconhecido deve ser estritamente positivo;
2. reconciliação de incidente deve ser atômica e idempotente;
3. mudanças de moeda de orçamento devem preservar integridade monetária e nunca misturar moedas sem FX;
4. `settle()` deve rejeitar estados/custos semanticamente contraditórios;
5. preservar todas as correções v1.0.1–v1.0.5;
6. repetir auditoria independente antes da Baseline Day 0.

