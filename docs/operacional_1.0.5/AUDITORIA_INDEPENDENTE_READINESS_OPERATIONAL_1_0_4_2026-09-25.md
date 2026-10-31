# AUDITORIA INDEPENDENTE DE READINESS — v1.0.4
MULTIVERSO GROWTH STUDIO  
Data: 2026-09-25

## VEREDITO

**NÃO APROVADA PARA READINESS AINDA.**

A v1.0.4 corrigiu os quatro pontos específicos que motivaram a v1.0.3, mas a auditoria adversarial independente encontrou **quatro bloqueadores operacionais adicionais**, todos reproduzíveis.

A baseline técnica v1.0.0 continua separada e permanece com o mesmo SHA-256 já aprovado.

---

## 1. Pacote auditado

Arquivo:

`MULTIVERSO_GROWTH_STUDIO_OPERATIONAL_1.0.4_CANDIDATE.zip`

SHA-256 independente:

`b49238a0847e680f41b15bdd0a312d3ecdc3e24164af31c0a7d74666a76acc72`

Integridade:

- ZIP: **PASS** (`testzip`: nenhum membro corrompido)
- entradas no ZIP: **508**
- inventário interno: **507/507 hashes corretos**
- 0 ausentes
- 0 divergentes
- o único arquivo não listado no inventário é o próprio `docs/INVENTARIO_SHA256.txt`
- `compileall`: **PASS**

Baseline v1.0.0 reconferida nesta auditoria:

`932292a9835fa8ca3b42515cfb356f01e96853c3be7659ae46ff37e800bb42fe`

---

## 2. Testes independentes executados

Foram executadas e concluídas independentemente:

- `tests.test_operacional_1_0_4`: **16/16 PASS**
- `tests.test_operacional_1_0_3` + `tests.test_operacional_1_0_2`: **24/24 PASS**
- `tests.test_operacional_1_0_1`: **24/24 PASS**
- `tests.test_correcao_auditoria_independente` + `tests.test_correcao_reauditoria_migracao_replay`: **18/18 PASS**

Total focado independente:

**82/82 PASS — 0 FAIL — 0 ERROR**

O pacote contém um relatório de regressão global com:

- 2.113 testes
- 2.111 PASS
- 0 FAIL
- 0 ERROR
- 2 SKIP
- RC=0

Esse resultado global **não é apresentado como reprodução independente** nesta auditoria. O arquivo empacotado informa que essa regressão levou aproximadamente 289 segundos; a auditoria independente concentrou a reprodução completa nos testes focados e nos ataques adversariais descritos abaixo.

---

# EVIDÊNCIA E CLASSIFICAÇÃO

Para evitar qualquer mistura entre operação real, mock e inferência:

- **EXECUÇÃO REAL LOCAL:** requisições HTTP reais por socket contra servidor loopback controlado pela auditoria, além de SQLite real.
- **ESTRUTURAL:** comportamento demonstrado no código/caminho de execução sem plataforma externa real.
- **NÃO VERIFICADO EXTERNAMENTE:** YouTube, Meta, TikTok, Hotmart, gateways, LLMs, APIs pagas e demais integrações reais continuam sem validação nesta auditoria.

Nenhuma chamada a plataforma externa real foi tratada como comprovada.

---

# BLOQUEADOR 1 — ENTRADA DIRETA `api/server.py` INICIA SERVIDOR SEM ATIVAR AS GUARDAS OPERACIONAIS

**Criticidade: CRÍTICA**  
**Evidência: EXECUÇÃO REAL LOCAL**

O caminho recomendado `main.py serve` chama `_configure_operational()` e ativa/configura kill switch e CostGuard.

Entretanto, `api/server.py` continua sendo um entrypoint executável independente:

`python api/server.py`

e `run_server(app=None)` chama `build_app()` diretamente, sem executar `_configure_operational()`.

### Reprodução independente

1. kill switch e CostGuard resetados;
2. `APP_ENV` não definido como production;
3. `api.server.run_server()` executado de verdade;
4. após a inicialização:
   - kill switch = `NOT_CONFIGURED_LIBRARY_MODE`
   - `external_execution_allowed = true`
   - CostGuard global = `None`
5. foi criado um `HttpSearchProvider(paid=True)`;
6. foi feita uma requisição HTTP real contra servidor loopback.

Resultado:

- request realmente saiu;
- servidor recebeu `POST /search`;
- provider retornou `available=True`;
- nenhum limite de custo foi exigido.

### Impacto

Há um segundo caminho executável do servidor capaz de iniciar a aplicação sem a configuração operacional fail-closed.

### Correção necessária

Uma das duas opções deve ser obrigatória:

1. remover/bloquear o entrypoint operacional direto de `api/server.py`; ou
2. fazer `run_server()` configurar explicitamente a camada operacional quando construir a própria aplicação.

Não depender apenas de documentação dizendo para usar `main.py serve`.

---

# BLOQUEADOR 2 — LIMITE DO OWNER = ZERO PODE EXECUTAR PROVIDER PAGO QUANDO A ESTIMATIVA E O CUSTO REPORTADO SÃO ZERO/DESCONHECIDOS

**Criticidade: CRÍTICA**  
**Evidência: EXECUÇÃO REAL LOCAL**

O CostGuard aceita `estimated_cost = 0.0`.

Quando o provider pago executa e não devolve `cost`, `settle()` grava:

- `cost_status = COST_UNKNOWN`
- `budget_charge = valor_reservado`

Se o valor reservado foi zero, o ledger continua em zero, nenhum incidente é aberto e novas chamadas continuam permitidas.

### Reprodução independente

Owner configurou:

`provider_total = USD 0.00`

para:

- `http_search_provider`
- `http_generation_provider`

Em seguida foram feitas duas chamadas HTTP reais contra servidor loopback, ambas com:

`estimated_cost = 0.0`

Os servidores responderam com sucesso, mas **sem campo de custo**.

Resultado:

- Search: executou
- Generation: executou
- servidor recebeu `/search` e `/gen`
- `calls_cost_unknown = 1` para cada provider
- `budget_charged = 0.0`
- nenhum incidente aberto

Ou seja: um limite de gasto explicitamente configurado como **zero** não impediu chamadas classificadas como pagas.

### Causa-raiz

A governança usa estimativa fornecida pelo chamador como reserva, mas custo real desconhecido não abre incidente quando a reserva é zero ou subestimada.

### Correção necessária

Para provider `PAID`:

- não permitir que `estimated_cost=0` funcione como autorização genérica;
- custo desconhecido após execução deve exigir política explícita;
- aceitar custo desconhecido somente quando existir uma **reserva máxima/upper bound confiável**, definida pelo provider/política do Owner, e não apenas por parâmetro arbitrário do chamador;
- se não houver upper bound confiável, abrir incidente e bloquear novas chamadas até reconciliação.

---

# BLOQUEADOR 3 — INCIDENTE DE DIVERGÊNCIA DE MOEDA PODE SER “RESOLVIDO” SEM RECONCILIAR O VALOR FINANCEIRO

**Criticidade: CRÍTICA**  
**Evidência: SQLITE REAL**

A v1.0.4 agora cria corretamente `CURRENCY_MISMATCH_INCIDENT`.

Porém `resolve_incident()` apenas marca o incidente como `RESOLVED`.

Ele não exige nem executa reconciliação financeira do lançamento relacionado.

### Reprodução independente

1. limite: USD 10
2. reserva: USD 1
3. custo real reportado: EUR 100
4. resultado:
   - `CURRENCY_MISMATCH`
   - `budget_charge = USD 1`
   - incidente aberto
   - próximas chamadas bloqueadas

Até aqui: correto.

Depois:

5. Owner executa `resolve_incident()` somente com texto de evidência;
6. o ledger continua:
   - custo conhecido: 0
   - cobrança no orçamento: USD 1
7. nova reserva USD 1 passa a ser:

`AUTHORIZED`

### Impacto

O provider volta a operar mesmo que o custo real de EUR 100 nunca tenha sido reconciliado ou convertido por fonte FX válida.

### Correção necessária

Incidentes que afetam integridade financeira não devem ser resolvidos apenas por mudança de status.

Para `CURRENCY_MISMATCH`, `INVALID_REPORTED_COST`, `SETTLEMENT_FAILED` e equivalentes:

- exigir reconciliação financeira explícita;
- registrar valor/moeda final comprovados ou decisão de write-off;
- registrar fonte/evidência;
- somente depois liberar o provider.

---

# BLOQUEADOR 4 — REPLAY DIVERGENTE DE `settle()` É ACEITO COMO “ALREADY_SETTLED”

**Criticidade: ALTA/CRÍTICA PARA INTEGRIDADE FINANCEIRA**  
**Evidência: SQLITE REAL**

A mesma `reservation_id` pode receber uma segunda liquidação com conteúdo divergente.

### Reprodução independente

Primeira liquidação:

- reservation: `rsv_*`
- outcome: EXECUTED
- actual_cost: USD 1
- resultado: `COMMITTED`, cobrança USD 1

Segunda liquidação da **mesma reservation_id**:

- outcome: EXECUTED
- actual_cost: USD 100

Resultado:

`already_settled = true`

Nenhum conflito é levantado.  
Nenhum incidente é aberto.  
O ledger continua com USD 1.

### Impacto

Idempotência legítima e replay divergente não são distinguidos.

A regra correta deve ser:

- mesma reservation_id + mesmo conteúdo comprovável = retry idempotente;
- mesma reservation_id + conteúdo divergente = conflito/incidente;
- conteúdo original insuficiente = fail-safe/revisão.

### Correção necessária

Persistir fingerprint/assinatura dos campos relevantes da liquidação ou comparar deterministamente com o registro persistido antes de retornar `already_settled`.

---

# OBSERVAÇÃO ESTRUTURAL — BROWSER AUTHENTICATED ACTION

**Classificação: ESTRUTURAL, NÃO VALIDADA COM CHROMIUM REAL NESTE AMBIENTE**

O Chromium gerenciado pelo Playwright não está instalado no ambiente desta auditoria.

Por inspeção e teste estrutural controlado, `execute_authenticated_action()`:

1. consulta kill switch;
2. faz `page.goto()`;
3. executa `page.evaluate(action_script)` sem nova checagem imediatamente antes da ação mutável.

Em teste estrutural, o kill switch foi ativado durante `page.goto`; mesmo com o estado final `ENABLED`, `page.evaluate()` ainda foi chamado e o método retornou sucesso.

Isso deve ser corrigido antes de liberar RPA/browser real, mas não é apresentado como prova de mutação em plataforma externa porque Chromium/plataforma real não foram executados nesta auditoria.

---

# O QUE A v1.0.4 CORRIGIU DE VERDADE

Os ataques focados confirmaram as correções anteriores:

- kill switch não configurado bloqueia em `APP_ENV=production` e no runtime marcado como operacional;
- a checagem final foi movida para o transporte central `egress.urlopen`;
- divergência de moeda abre incidente;
- `HttpSearchProvider` exige classificação de custo;
- provider pago sem limite é bloqueado;
- valores negativos, NaN, infinito e extremos inválidos continuam bloqueados;
- corrida concorrente de reserva continua protegida;
- overrun continua abrindo incidente.

Esses pontos foram reproduzidos pelos testes independentes focados.

---

# O QUE NÃO FOI VALIDADO

Ainda **não** foi comprovado em operação externa real:

- YouTube Data API
- Meta Graph API
- TikTok API
- Hotmart/Monetizze/Eduzz/Kiwify/Braip
- gateway de pagamento
- analytics externo
- providers reais de vídeo
- LLM pago
- providers reais de pesquisa
- modelo de visão
- RADAR com egress real de internet
- Chromium/Playwright real

Esses itens continuam pendentes e não são tratados como sucesso por esta auditoria.

---

# DECISÃO

**NÃO avançar ainda para Baseline Day 0 nem piloto real.**

Próxima candidata recomendada:

`v1.0.5-OPERATIONAL-CANDIDATE`

Escopo estritamente focal:

1. fechar o entrypoint alternativo `api/server.py`;
2. impedir bypass de orçamento com estimativa zero + custo real desconhecido;
3. exigir reconciliação financeira antes de resolver incidentes de custo;
4. distinguir retry idempotente de settlement replay divergente;
5. adicionar rechecagem do kill switch imediatamente antes de `page.evaluate()` e validar depois com Chromium real;
6. preservar todas as correções anteriores;
7. nova auditoria independente antes da Baseline Day 0.
