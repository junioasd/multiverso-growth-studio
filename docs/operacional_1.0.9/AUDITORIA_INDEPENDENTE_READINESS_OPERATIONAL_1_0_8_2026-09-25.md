# AUDITORIA INDEPENDENTE DE READINESS — v1.0.8
MULTIVERSO GROWTH STUDIO
Data: 2026-09-25

## VEREDITO

**NÃO APROVADA PARA READINESS AINDA.**

A v1.0.8 corrigiu, nos cenários reproduzidos, os quatro bloqueadores da auditoria da v1.0.7. A auditoria adversarial independente encontrou, porém, quatro lacunas adicionais de integridade/liveness financeiro antes da Baseline Day 0.

Nenhuma integração externa de mercado foi tratada como validada.

---

## PACOTE AUDITADO

`MULTIVERSO_GROWTH_STUDIO_OPERATIONAL_1.0.8_CANDIDATE.zip`

SHA-256 independente:

`9ab6e1511b355b89040108a21e63860c68d5362617483f4e075be3ab9c89a95a`

Integridade:

- ZIP: PASS (`testzip`: nenhum membro corrompido)
- entradas no ZIP: 537
- inventário interno: **536/536 hashes corretos**
- ausentes: 0
- divergentes: 0
- `compileall`: PASS

---

## TESTES INDEPENDENTES REPRODUZIDOS

- `tests.test_operacional_1_0_8`: 19/19 PASS
- `tests.test_operacional_1_0_7`: 17/17 PASS
- `tests.test_operacional_1_0_6`: 19/19 PASS
- `tests.test_operacional_1_0_5`: 13/13 PASS
- `tests.test_operacional_1_0_4`: 16/16 PASS
- `tests.test_operacional_1_0_3`: 11/11 PASS

Total reproduzido nesta rodada:

**95/95 PASS — 0 FAIL — 0 ERROR**

A regressão completa empacotada declara 2.181 testes / 2.179 PASS / 0 FAIL / 0 ERROR / 2 SKIP. Esse total global é evidência do construtor e **não é apresentado aqui como reprodução independente integral**.

---

## O QUE A v1.0.8 CORRIGIU DE VERDADE

Foram confirmados nos testes focados:

1. `ZZZ` deixou de ser aceita como moeda operacional.
2. conversão FX que produziria valor acima de `MAX_MONEY` é recusada e sofre rollback.
3. write-off de conflito encerra a reserva em estado terminal `WRITTEN_OFF`.
4. uso sem custo cria incidente ligado ao `usage_id` e existe `reconcile_usage()`.

Esses pontos estão corrigidos nos cenários cobertos.

---

# BLOQUEADOR 1 — MOEDA LEGADA NÃO SUPORTADA COM HISTÓRICO FINANCEIRO FICA SEM CAMINHO DE RECUPERAÇÃO

**Criticidade: ALTA / LIVENESS FINANCEIRO**
**Evidência: SQLITE REAL + API PÚBLICA DO COSTGUARD**

A v1.0.8 diz que moeda legada fora da política falha fechada e, havendo histórico, exige reconciliação.

A auditoria reproduziu o seguinte estado legado:

- limite `provider_total = 100 ZZZ`;
- uso confirmado = `5 ZZZ`.

Resultado:

- `preview(... currency="ZZZ")` → `INVALID_CURRENCY`;
- `retire_unsupported_currency_limits()` → recusa porque existe histórico;
- `migrate_budget_currency(ZZZ -> USD)` → recusa porque `ZZZ` não é moeda suportada;
- não existe incidente aberto;
- `reconcile_usage()` → não encontra incidente ligado ao uso;
- não existe reserva a reconciliar.

Ou seja: o sistema informa que é necessária reconciliação, mas não fornece um fluxo operacional público para realizá-la.

### Correção necessária

Criar um fluxo explícito para legado em moeda retirada/não suportada, por exemplo:

- `reconcile_unsupported_currency_history()`; ou
- incidente de upgrade específico por lançamento;
- Owner informa moeda destino, FX real, fonte, timestamp e evidência;
- materialização/reconciliação atômica e auditável;
- só depois os limites antigos podem ser retirados.

Fail-closed sem caminho de recuperação vira bloqueio permanente.

---

# BLOQUEADOR 2 — RETIRADA DE MOEDA EXTRA IGNORA `converted_currency` E PODE APAGAR LIMITES COM HISTÓRICO EFETIVO NESSA MOEDA

**Criticidade: CRÍTICA PARA INTEGRIDADE DE MIGRAÇÃO**
**Evidência: SQLITE REAL**

Cenário executado usando um caminho permitido pela própria política:

1. `MGS_EXTRA_CURRENCIES=JPY`;
2. provider opera em USD e registra gasto real local no ledger;
3. migração USD→JPY materializa o histórico:
   - `currency = USD`
   - `converted_currency = JPY`
   - limite passa para JPY;
4. posteriormente JPY deixa de estar na política ativa;
5. `retire_unsupported_currency_limits()` é chamado.

A função verifica o histórico apenas pela coluna nativa `currency = JPY`, não pelo valor efetivo:

`COALESCE(converted_currency, currency)`.

Resultado reproduzido:

- o limite JPY foi removido;
- o histórico convertido em JPY permaneceu;
- o provider ficou sem limites configurados;
- nova operação retorna `PAID_PROVIDER_NOT_AUTHORIZED`.

### Correção necessária

Toda análise de histórico para retirada de moeda deve usar a moeda **efetiva**, exatamente como os cálculos de orçamento:

`COALESCE(converted_currency, currency)`.

Também deve considerar:

- reservas;
- incidentes;
- migrações materializadas;
- cadeia FX;
- política removida após restart.

Nunca apagar a configuração de uma moeda enquanto existir histórico financeiro efetivo nela sem uma migração/reconciliação completa.

---

# BLOQUEADOR 3 — WRITE-OFF DE USO SEM CUSTO UTILIZA O UPPER BOUND ATUAL, NÃO O VIGENTE QUANDO O USO FOI REGISTRADO

**Criticidade: CRÍTICA PARA AUDITORIA FINANCEIRA**
**Evidência: SQLITE REAL**

A auditoria configurou:

- `provider_total = USD 1000`;
- `unknown_cost_upper_bound = USD 100`.

Em seguida foi registrado um uso sem custo conhecido, criando corretamente `USAGE_COST_UNKNOWN`.

Antes do write-off, o Owner alterou:

`unknown_cost_upper_bound: 100 -> 1`.

Depois:

`reconcile_usage(... write_off=True)`

foi aceito.

Resultado:

- o uso foi encerrado como `WRITTEN_OFF`;
- `budget_charge = USD 1`.

Assim, o valor contabilizado para um evento histórico depende da **configuração atual** no momento da reconciliação, e não da política/teto vigente quando o uso ocorreu.

### Impacto

Uma mudança de política posterior pode reescrever economicamente um evento passado sem declarar uma reconciliação de valor.

### Correção necessária

Quando um uso sem custo for criado:

- persistir o upper bound aplicável naquele instante (`reserved_upper_bound` / `policy_snapshot`);
- persistir moeda e versão/política;
- write-off deve usar esse snapshot histórico;
- alteração posterior do limite não pode modificar o valor conservador de um uso já ocorrido;
- se não existir snapshot legado, exigir valor final/evidência ou incidente de revisão específico.

---

# BLOQUEADOR 4 — `record_usage()` PODE LIQUIDAR A RESERVA ERRADA QUANDO EXISTEM CHAMADAS CONCORRENTES IGUAIS

**Criticidade: ALTA / INTEGRIDADE DE CORRELAÇÃO**
**Evidência: SQLITE REAL**

Quando `record_usage()` é chamado sem `reservation_id`, o método procura a primeira reserva aberta com:

- provider;
- service;
- operation;
- project;
- campaign;

e escolhe a mais antiga.

Ele não possui uma identidade inequívoca da chamada.

### Reprodução independente

Foram criadas duas reservas do mesmo:

- provider `p`;
- service `s`;
- operation `o`;

com valores:

- reserva A = USD 10;
- reserva B = USD 100.

O resultado real correspondente à reserva B (USD 100) foi entregue primeiro via:

`record_usage(... cost=100, reservation_id=None)`.

O CostGuard vinculou esse uso à **reserva A de USD 10**.

Resultado:

- reserva A foi marcada `COMMITTED` com custo USD 100;
- reserva B permaneceu `RESERVED`;
- foi criado um `COST_OVERRUN` artificial para a reserva A.

### Impacto

Duas operações concorrentes semanticamente iguais podem ter custo, estado e incidentes trocados entre si.

### Correção necessária

Não adivinhar a reserva correspondente.

Uma destas políticas:

- exigir `reservation_id` quando existir reserva aberta; ou
- usar correlation/idempotency key única persistida desde a reserva até o settlement;
- se houver mais de um candidato possível, retornar `AMBIGUOUS_RESERVATION_MATCH` e falhar fechado.

O matching por “primeira reserva mais antiga” não é seguro para operação concorrente.

---

## DISTINÇÃO DE EVIDÊNCIA

Nesta auditoria foram realmente executados:

- código Python do pacote;
- SQLite real;
- transações e migrações locais;
- chamadas públicas do CostGuard;
- testes locais.

Continuam **NÃO VERIFICADOS EXTERNAMENTE**:

- YouTube;
- Meta/Facebook/Instagram;
- TikTok;
- Hotmart/Monetizze/Eduzz/Kiwify/Braip;
- gateways reais;
- providers pagos reais;
- Chromium/Playwright real;
- cotações FX reais;
- demais integrações externas.

Nenhuma dessas integrações é declarada funcional por esta auditoria.

---

## DECISÃO

**NÃO avançar ainda para Baseline Day 0 nem piloto real.**

Próxima candidata recomendada:

`v1.0.9-OPERATIONAL-CANDIDATE`

Escopo focal:

1. criar caminho auditável de recuperação para histórico em moeda retirada/não suportada;
2. corrigir retirada de moeda para considerar `converted_currency`/moeda efetiva;
3. congelar o upper bound/política aplicável no instante do uso desconhecido;
4. eliminar associação ambígua de `record_usage()` com reservas concorrentes;
5. preservar todas as correções v1.0.1–v1.0.8;
6. nova auditoria independente antes da Baseline Day 0.
