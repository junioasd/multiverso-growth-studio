# CORREÇÃO DA AUDITORIA DE READINESS 1.0.6 → 1.0.7-OPERATIONAL-CANDIDATE

- **Auditoria:** `AUDITORIA_INDEPENDENTE_READINESS_OPERATIONAL_1_0_6_2026-09-25.md` (nesta pasta), com veredito **NÃO APROVADA** e 4 bloqueadores.
- **Predecessores (inalterados):** 1.0.6 `2109b3e2…f449f`, 1.0.5 … 1.0.1, v1.0.0 `932292a9…e42fe`.
- **Status:** CANDIDATE — nova auditoria independente de readiness necessária.

## Classificação da evidência
- **EXECUTADO_REALMENTE:** SQLite real, threads reais, código Python e *triggers* SQLite abortando transações (crash simulado **dentro** da transação real).
- **TESTADO_LOCALMENTE:** as duas regressões completas e a mutação.
- **NÃO_VERIFICADO / DEPENDENTE_DE_AMBIENTE_EXTERNO:** provedores pagos reais, gateways, plataformas, Chromium/Playwright real e FX de fonte real. As taxas nos testes são valores de teste, **não cotações reais**.

## Resultado
| | 1.0.6 (auditada) | 1.0.7 |
|---|---|---|
| Testes | 2.145 | 2.162 (+17) |
| PASS / FAIL / ERROR / SKIP | 2.143 / 0 / 0 / 2 | 2.160 / 0 / 0 / 2 |
| B1 `set_limit` concorrente USD × EUR | **as duas gravadas; moedas misturadas** | uma vence; a outra `BUDGET_CURRENCY_CONFLICT` |
| B2 USD→EUR repetido a 0,5 | **EUR 65 autorizado** (saldo real era 45) | `FX_HISTORY_ALREADY_MIGRATED`; EUR 65 bloqueado |
| B3 cadeia USD→EUR→GBP | **provedor inoperante** | operante; histórico USD 50 = GBP 36 (×0,72) |
| B4 EUR 100 × 0,5 com final USD 1 | **aceito** | `FX_RECONCILIATION_MISMATCH` |

- **Evidência:** `reproducao_bloqueadores_readiness_1.0.6.py`, com saídas `reproducao_ANTES_1.0.6.txt` e `reproducao_DEPOIS_1.0.7.txt`.
- **Leitura da linha "ledger m = 1.0":** no ANTES é o resultado da reconciliação incoerente **aceita**; no DEPOIS é a cobrança da reserva que **permanece** porque a reconciliação foi recusada.

## Causas-raiz
1. **B1:** a validação de moeda (`_assert_currency_consistent`) e a gravação estavam em transações separadas (TOCTOU).
2. **B2 e B3 (mesma raiz):** a conversão do histórico era **recalculada a cada leitura** pela "migração mais recente". Toda migração nova reinterpretava o passado, e cadeias não compunham.
3. **B4:** a taxa FX era gravada como metadado e nunca entrava na conta.

## Correções
1. **`set_limit`:** validação e gravação numa única transação `BEGIN IMMEDIATE`, com uma única moeda operacional por provedor. Um concorrente em outra moeda recebe `BUDGET_CURRENCY_CONFLICT`.
2. **Migração materializada por registro** (estratégia escolhida para a cadeia: **opção B com composição determinística**):
   - a migração, em uma transação `BEGIN IMMEDIATE`, grava em **cada lançamento** o valor efetivo na nova moeda (`converted_amount`/`converted_currency`), o id da migração e a **cadeia aplicada** (`#1 USD->EUR@0.9 > #2 EUR->GBP@0.8`);
   - a leitura soma os valores efetivos e **nenhuma taxa é reaplicada**: migração nova nunca reinterpreta o passado;
   - só se migra **a partir da moeda operacional atual**;
   - retry idêntico é idempotente; o par já migrado com conteúdo diferente gera `FX_HISTORY_ALREADY_MIGRATED`; migração que não parte da moeda atual gera `FX_MIGRATION_CONFLICT` **antes** de qualquer mudança;
   - crash no meio da migração desfaz tudo (histórico, limites e registro).
3. **Reconciliação FX:**
   - o incidente de moeda guarda `original_amount`/`original_currency` **estruturados**;
   - a reconciliação exige `|original × fx_rate + fees + adjustments − final_amount| ≤ 0,01`, na moeda do orçamento, e grava taxas, ajustes e valor esperado;
   - `fees` precisam ser ≥ 0; `adjustments` são reais finitos e podem ser negativos (desconto);
   - o erro é `FX_RECONCILIATION_MISMATCH`;
   - incidente legado sem original estruturado não pode ser reconciliado por FX (só por write-off explícito).

## Revisão da classe — variantes encontradas e corrigidas
- **V1:** `record_usage` direto aceitava custo em moeda diferente da operacional (outra porta para misturar moedas). Agora gera `BUDGET_CURRENCY_CONFLICT`.
- **V2:** banco vindo da 1.0.6 com migração registrada **sem materialização**. Fica em fail-closed, e `materialize_legacy_migration` (Owner, com evidência) aplica a migração legada **somente** se ela for a única do par. O cenário exato da auditoria (USD→EUR a 0,9 e 0,5) é recusado como ambíguo (`FX_MIGRATION_CONFLICT`).
- **V3:** o teste de concorrência de reconciliação da 1.0.6 teria ficado **silenciosamente enfraquecido**: 5 das 6 threads passariam a falhar pela validação FX, e não pela disputa. Foi ajustado para reconciliações **matematicamente válidas** (variação por `fees` explícitas), e agora exige que os 5 perdedores percam **por concorrência**.
- **V4:** incidentes de moeda abertos deixaram de contar como "histórico estrangeiro" no `set_limit` (já bloqueiam o provedor pelo incidente), evitando bloqueio duplo de configuração na moeda do próprio orçamento.

## Arquivos
- **Produção:**
  - `core/operations/cost_guard.py`
  - `core/models/db.py` (colunas aditivas: `converted_amount`, `converted_currency`, `fx_migration_id`, `fx_chain`, `migration_fingerprint`, `original_amount`, `original_currency`, `fees`, `adjustments`, `expected_amount`)
- **Testes:**
  - novo `tests/test_operacional_1_0_7.py` (17 testes, cobrindo os itens 1–32 e as variantes);
  - ajustado `tests/test_operacional_1_0_6.py::ReconciliationTests` (V3 acima; a intenção foi preservada e reforçada).

## Testes e mutação
- **Suítes:** 1.0.1–1.0.7, correções de auditoria e replay legado verdes; runner estrito RC 0.
- **Regressão completa:** 2.162 / 2.160 PASS / 0 FAIL / 0 ERROR / 2 SKIP.
- **Mutação: 12 de 12 detectados, cada um pelo teste esperado.** Um passou cego na 1ª rodada ("legado materializa com taxa ambígua") e revelou **lacuna real de cobertura**: o teste de upgrade tinha só uma migração legada. Fechada com o cenário exato da auditoria.

## Pendências
- **Internas:** nenhuma identificada, fora a validação do navegador com Chromium real.
- **Exclusivas do Owner:** as anteriores; taxas FX **reais** (fonte e horário) em qualquer migração ou reconciliação de moeda; para bancos vindos da 1.0.6 com migração registrada, a materialização explícita.
- **Exclusivas de serviços externos:** as 12 integrações, incluindo Chromium/Playwright real; nenhuma conectada.
