# AUDITORIA INDEPENDENTE DE READINESS — v1.0.6
MULTIVERSO GROWTH STUDIO
Data: 2026-09-25

## VEREDITO

**NÃO APROVADA PARA READINESS AINDA.**

A v1.0.6 corrige de forma reproduzível os quatro bloqueadores apontados na auditoria da v1.0.5, e a regressão focada anterior permaneceu verde. Porém, a auditoria adversarial independente encontrou **quatro novos bloqueadores de integridade monetária/concorrência** antes da Baseline Day 0.

Nenhuma integração externa de mercado foi considerada validada nesta auditoria.

---

## PACOTE AUDITADO

`MULTIVERSO_GROWTH_STUDIO_OPERATIONAL_1.0.6_CANDIDATE.zip`

SHA-256 independente:

`2109b3e2116d6245c43012aa9dc86cecf66bc1fcb3fbac88b5a81d51b17f449f`

Integridade:

- ZIP: PASS (`testzip`: nenhum membro corrompido)
- 523 entradas
- inventário interno: **522/522 hashes corretos**
- 0 divergências
- 0 ausentes
- `compileall`: PASS

---

## TESTES INDEPENDENTES FOCADOS

Executados diretamente no pacote recebido:

- `tests.test_operacional_1_0_6`: 19/19 PASS
- `tests.test_operacional_1_0_5`: 13/13 PASS
- `tests.test_operacional_1_0_4`: 16/16 PASS
- `tests.test_operacional_1_0_3`: 11/11 PASS
- `tests.test_operacional_1_0_2`: 13/13 PASS
- `tests.test_operacional_1_0_1`: 24/24 PASS
- `tests.test_correcao_auditoria_independente`: 9/9 PASS
- `tests.test_correcao_reauditoria_migracao_replay`: 9/9 PASS

Total focado independente:

**114/114 PASS — 0 FAIL — 0 ERROR**

O pacote informa regressão global de 2.145 testes, 2.143 PASS, 0 FAIL, 0 ERROR, 2 SKIP. Esse total global é evidência do construtor; não é apresentado aqui como reprodução independente integral.

---

## O QUE A v1.0.6 CORRIGIU DE VERDADE

Os cenários focados confirmam:

1. `unknown_cost_upper_bound=0` é rejeitado e zero legado não é tratado como teto válido.
2. reconciliações concorrentes do mesmo incidente agora têm apenas um vencedor.
3. mudança direta de moeda com histórico é bloqueada.
4. settlement contraditório preserva o custo e abre incidente.
5. variantes de reconcile/replay/overrun cobertas pela nova suíte permanecem protegidas.

Essas correções são reais nos cenários testados.

---

# BLOQUEADOR 1 — `set_limit()` PODE CRIAR LIMITES DO MESMO PROVIDER EM MOEDAS DIFERENTES SOB CONCORRÊNCIA

**Criticidade: CRÍTICA**
**Evidência: SQLITE REAL + THREADS REAIS**

A verificação de consistência de moeda (`_assert_currency_consistent`) ocorre em uma conexão/transação separada do `INSERT/UPDATE` do limite.

Duas chamadas concorrentes podem verificar o estado vazio ao mesmo tempo e depois gravar limites em moedas diferentes.

### Reprodução independente

Duas threads reais foram liberadas simultaneamente:

- `provider_total = USD 100`
- `per_call = EUR 100`

As duas chamadas retornaram sucesso.

Estado final persistido:

- `per_call` → EUR
- `provider_total` → USD

Depois disso:

- preview em USD → `CURRENCY_MISMATCH`
- preview em EUR → `CURRENCY_MISMATCH`

O provider entra em estado internamente inconsistente.

### Causa-raiz

TOCTOU: validar e persistir não fazem parte da mesma transação serializada.

### Correção necessária

`set_limit()` deve:

- usar transação única (`BEGIN IMMEDIATE` ou equivalente);
- validar a moeda e gravar o limite sob o mesmo lock;
- garantir invariantemente uma única moeda operacional por provider;
- adicionar teste concorrente com limites de tipos diferentes.

---

# BLOQUEADOR 2 — REPETIR A MESMA MIGRAÇÃO DE MOEDA COM OUTRA TAXA ALTERA RETROATIVAMENTE O GASTO HISTÓRICO SEM ALTERAR O LIMITE

**Criticidade: CRÍTICA**
**Evidência: SQLITE REAL**

A função `_committed()` usa a migração mais recente para converter todo histórico antigo daquela moeda.

`migrate_budget_currency()` permite registrar novamente o mesmo par `USD -> EUR`, mesmo quando os limites já estão em EUR.

### Reprodução independente

Estado inicial:

- limite USD 100
- gasto histórico USD 50

Migração 1:

- USD → EUR
- FX = 0,9
- limite vira EUR 90
- gasto convertido = EUR 45
- nova chamada EUR 45,01 é corretamente bloqueada.

Migração 2, no mesmo par:

- USD → EUR
- FX = 0,5
- nenhum limite USD existia para converter novamente;
- limite permanece EUR 90;
- porém `_committed()` passa a converter o histórico USD 50 pela taxa mais recente, resultando em EUR 25.

Após a segunda migração, uma nova chamada de **EUR 65 foi autorizada**, embora antes o saldo disponível fosse apenas EUR 45.

### Impacto

Uma segunda migração altera retroativamente o orçamento disponível sem uma reconciliação/migração coerente dos limites.

### Correção necessária

A migração precisa ser versionada/idempotente e temporalmente consistente.

No mínimo:

- rejeitar repetição do mesmo `from_currency -> to_currency` quando já houver migração aplicável ao histórico;
- ou persistir qual taxa de migração pertence a quais registros/períodos;
- nunca usar simplesmente “a taxa mais recente” para reavaliar todo o histórico anterior.

---

# BLOQUEADOR 3 — MIGRAÇÃO ENCADEADA DE MOEDA DEIXA O PROVIDER BLOQUEADO

**Criticidade: ALTA/CRÍTICA PARA OPERAÇÃO MULTIMOEDA**
**Evidência: SQLITE REAL**

O mecanismo suporta uma migração direta, mas não compõe cadeias.

### Reprodução independente

1. histórico: USD 50; limite USD 100
2. migração USD → EUR a 0,9
3. sistema opera corretamente em EUR
4. migração EUR → GBP a 0,8
5. limite passa para GBP 72

Porém o histórico original continua em USD e só existe:

- USD → EUR
- EUR → GBP

Não existe uma migração direta USD → GBP.

Resultado do preview em GBP:

`BUDGET_CURRENCY_CHANGE_REQUIRES_RECONCILIATION`

Ou seja, a própria segunda migração aceita pelo sistema produz um estado no qual o provider não pode operar.

### Correção necessária

Uma das políticas precisa ser implementada:

- compor deterministicamente a cadeia FX com histórico/auditoria;
- materializar uma migração consolidada do histórico;
- ou bloquear a segunda migração antes de executá-la, exigindo um processo explícito compatível.

O sistema nunca deve aceitar uma migração e só depois descobrir que o estado resultante é não operacional.

---

# BLOQUEADOR 4 — RECONCILIAÇÃO DE DIVERGÊNCIA DE MOEDA NÃO CONFERE COERÊNCIA MATEMÁTICA ENTRE CUSTO ORIGINAL, FX E VALOR FINAL

**Criticidade: CRÍTICA PARA INTEGRIDADE FINANCEIRA**
**Evidência: SQLITE REAL**

Para um `CURRENCY_MISMATCH_INCIDENT`, o sistema exige:

- valor final;
- moeda final;
- `fx_rate`;
- fonte FX;
- timestamp.

Porém a taxa FX informada não é usada para validar o valor final.

### Reprodução independente

Incidente criado por:

- custo reportado: EUR 100
- orçamento: USD

Reconciliação enviada pelo Owner:

- `final_amount = USD 1`
- `fx_rate = 0,5`
- fonte/timestamp/evidência presentes

Matematicamente, EUR 100 × 0,5 = USD 50.

Mesmo assim, a reconciliação foi aceita e o ledger terminou em:

`USD 1`

### Impacto

Os campos de FX podem virar metadados meramente decorativos; um valor final incompatível com a própria taxa registrada pode ser aceito como reconciliado.

### Correção necessária

Para incidentes cuja reconciliação alegue conversão FX:

- armazenar o valor/moeda original de forma estruturada;
- calcular/validar `original_amount × fx_rate`;
- comparar com `final_amount` dentro de tolerância explícita;
- taxas, fees ou ajustes adicionais precisam ser campos explícitos e auditáveis;
- incompatibilidade deve gerar `FX_RECONCILIATION_MISMATCH` ou equivalente.

Não aceitar metadados FX que contradigam o valor financeiro persistido.

---

## DISTINÇÃO DE EVIDÊNCIA

Nesta auditoria:

- **realmente executado:** SQLite, threads/concurrency, código Python, migrações e cálculos locais;
- **não validado externamente:** YouTube, Meta, TikTok, Hotmart, gateway, providers pagos reais, Chromium/Playwright real e demais integrações de mercado.

Nenhuma conclusão externa foi inferida a partir de mocks ou relatórios.

---

## DECISÃO

**NÃO avançar ainda para Baseline Day 0 nem piloto real.**

Próxima candidata recomendada:

`v1.0.7-OPERATIONAL-CANDIDATE`

Escopo focal:

1. tornar `set_limit()` atomicamente consistente sob concorrência;
2. tornar migrações de moeda idempotentes/temporais sem reprecificar retroativamente o histórico;
3. suportar ou bloquear corretamente migrações encadeadas antes de criar estado inconsistente;
4. validar coerência matemática da reconciliação FX;
5. preservar integralmente todas as correções v1.0.1–v1.0.6;
6. repetir auditoria independente antes da Baseline Day 0.
