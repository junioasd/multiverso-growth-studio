# CORREÇÃO DA AUDITORIA DE READINESS 1.0.8 → 1.0.9-OPERATIONAL-CANDIDATE

- **Auditoria:** `AUDITORIA_INDEPENDENTE_READINESS_OPERATIONAL_1_0_8_2026-09-25.md` (nesta pasta), com veredito **NÃO APROVADA** e 4 bloqueadores.
- **Predecessores (inalterados):** 1.0.8 `9ab6e151…a95a`, 1.0.7 … 1.0.1, v1.0.0 `932292a9…e42fe`.
- **Status:** CANDIDATE — nova auditoria independente de readiness necessária.

## Classificação da evidência
- **EXECUTADO_REALMENTE:** SQLite real, threads reais (6 resultados concorrentes fora de ordem), *triggers* abortando transações reais, reinícios com nova instância do `CostGuard`.
- **TESTADO_LOCALMENTE:** regressão completa e mutação.
- **SIMULADO:** a remoção de JPY da política (ambiente sem `MGS_EXTRA_CURRENCIES` após a migração) e os estados legados inseridos via SQL.
- **NÃO_VERIFICADO / DEPENDENTE_DE_AMBIENTE_EXTERNO:** provedores pagos reais, gateways, plataformas, Chromium/Playwright real e cotações FX reais. As taxas dos testes (2,0; 150; 0,0067) são valores de teste, **não cotações**.

## Resultado
| | 1.0.8 (auditada) | 1.0.9 |
|---|---|---|
| Testes | 2.181 | 2.197 (+16) |
| PASS / FAIL / ERROR / SKIP | 2.179 / 0 / 0 / 2 | 2.195 / 0 / 0 / 2 |
| B1 ZZZ com histórico | **sem caminho de recuperação** | `reconcile_unsupported_currency_history`; o provedor volta a operar |
| B2 USD→JPY, depois JPY retirado | **limite JPY apagado; histórico órfão** | `CURRENCY_RETIREMENT_REQUIRES_RECONCILIATION`; limite preservado |
| B3 teto 100 no evento, 1 no write-off | **cobrou 1** | cobrou 100 (snapshot do evento) |
| B4 resultado de B entregue sem id | **liquidou A; overrun artificial em A** | `AMBIGUOUS_RESERVATION_MATCH`; nada alterado |

**Evidência:** `reproducao_bloqueadores_readiness_1.0.8.py`, com saídas `reproducao_ANTES_1.0.8.txt` e `reproducao_DEPOIS_1.0.9.txt`.

## Causas-raiz
1. **B1:** a migração só aceita moeda de origem **suportada**, e não havia fluxo para histórico em moeda retirada (fail-closed sem saída).
2. **B2:** a aposentadoria conferia a coluna **nativa** `currency`, ignorando `converted_currency`.
3. **B3:** o write-off lia o teto **atual** em `ai_api_limits`, não o vigente no evento.
4. **B4:** `record_usage` sem id escolhia a reserva "mais antiga" (`ORDER BY created_at LIMIT 1`).

## Fluxo final de recuperação de moeda legada
`reconcile_unsupported_currency_history(principal, provider, legacy_currency, target_currency, fx_rate, fx_source, fx_timestamp, evidence, declared_converted_total, fees=0, adjustments=0)`:
- **Pré-condições:** Owner; moeda legada **não** suportada (se ainda for suportada, use a migração); destino suportado; FX > 0 finito, com fonte, horário e evidência.
- **Validação matemática:** |total efetivo legado × fx + fees + ajustes − total declarado| ≤ 0,01. Ajuste líquido negativo é recusado (conservador), e todo valor resultante deve estar em [0, 10¹²] sem underflow.
- **Recusa em ambiguidade:** reservas ou incidentes pendentes, limites numa terceira moeda, ou reconciliação anterior divergente (`FX_HISTORY_ALREADY_MIGRATED`).
- **Em uma transação `BEGIN IMMEDIATE`:** materializa cada lançamento pela moeda **efetiva** (a cadeia é estendida, não reescrita), lança fees/ajustes líquidos como `LEGACY_FX_ADJUSTMENT`, converte os limites legados, registra a migração com fingerprint e grava a trilha `LEGACY_CURRENCY_RECONCILED` (original, taxa, fonte, horário, total esperado).
- **Idempotência:** retry idêntico retorna `already_reconciled`; crash desfaz tudo.

## Regra oficial de moeda efetiva
- **Definição:** `EFFECTIVE_CURRENCY = COALESCE(converted_currency, currency)` e `EFFECTIVE_AMOUNT = COALESCE(converted_amount, budget_charge)`, centralizados como constantes.
- **Onde se aplica:** cálculos de orçamento, histórico estrangeiro, migração, materialização legada, recuperação legada, aposentadoria de moeda (que também considera reservas e incidentes abertos na moeda) e `summary(effective=True)`.
- **Compatibilidade:** o `summary()` padrão continua nativo.

## Estrutura do snapshot histórico de política
- **Onde é gravado:** ao registrar um uso sem custo, o incidente `USAGE_COST_UNKNOWN` recebe, **na mesma transação**:
  - `policy_snapshot` (JSON imutável): `provider`, `service`, `operation`, `currency` (moeda operacional), `unknown_cost_upper_bound` válido ou nulo, `limits` (`kind`, `scope_id`, `amount`, `currency`, `updated_at`), `at` e `policy_version` (hash determinístico dos limites);
  - `snapshot_upper_bound` e `snapshot_currency` (colunas normalizadas).
- **Write-off:** usa **somente** o snapshot. Evento sem snapshot (legado ou upgrade) gera `LEGACY_SNAPSHOT_MISSING` e exige valor final comprovado.
- **Moeda:** divergência entre a moeda do evento e a atual exige reconciliação de moeda antes.
- **Reservas:** o valor reservado já congela a política no momento da reserva (`max(estimativa, teto)`).

## Estratégia definitiva de correlação reserva↔uso
- `reserve()/authorize()` aceitam `correlation_id`, persistido com **índice único**. Repetir o mesmo `correlation_id` para a mesma chamada é idempotente; para outra chamada gera `CORRELATION_ID_CONFLICT`.
- `record_usage()` resolve a reserva por `reservation_id` ou `correlation_id`. **Sem identidade:** 0 candidatos = uso sem reserva; 1 candidato = associação inequívoca; **2+ = `AMBIGUOUS_RESERVATION_MATCH`**, sem alterar reservas, uso ou incidentes.
- Os caminhos dos provedores (`run_metered_call`) já liquidam pelo `reservation_id` exato.
- **Replay:** retry idêntico do uso é idempotente (fingerprint de liquidação); divergente gera `SettlementConflict`.

## Revisão da classe — variantes encontradas e corrigidas
- **V1:** `summary()` agrupava apenas pela moeda nativa, o que, depois de uma migração, divergia do cálculo de orçamento. Acrescentado `summary(effective=True)`.
- **V2:** a aposentadoria de moeda ignorava incidentes abertos na moeda. Agora os considera.
- **V3:** o write-off de uso sem custo não verificava a moeda do evento contra a moeda atual. Agora exige reconciliação de moeda antes.
- **V4:** a recuperação legada com o limite já existente na moeda destino poderia duplicar o limite. O legado sai e o vigente fica.

## Testes existentes ajustados (2, consequência direta; documentados no código)
- **`test_operacional_1_0_8::test_legacy_unknown_currency_with_financial_history_requires_reconciliation`:** o código esperado passou a `CURRENCY_RETIREMENT_REQUIRES_RECONCILIATION` (exigido pelo comando); a intenção é idêntica.
- **`test_operacional_1_0_8::test_write_off_of_unknown_usage_requires_trusted_bound`:** a expectativa antiga (teto definido **depois** do evento vale no write-off) **era o próprio bloqueador B3**. A nova expectativa usa o teto congelado no evento. A intenção original (write-off exige teto confiável e nunca presume zero) foi preservada e reforçada.

## Arquivos
- **Produção:**
  - `core/operations/cost_guard.py`
  - `core/models/db.py` (colunas aditivas `policy_snapshot`, `snapshot_upper_bound`, `snapshot_currency` e `correlation_id`, mais índice único parcial)
- **Testes:**
  - novo `tests/test_operacional_1_0_9.py` (16 testes, itens 1–39, incluindo concorrência real fora de ordem);
  - ajustado `tests/test_operacional_1_0_8.py` (2 testes, acima).

## Testes e mutação
- **Suítes:** 1.0.1–1.0.9, correções de auditoria e replay legado verdes; runner estrito RC 0.
- **Regressão completa:** 2.197 / 2.195 PASS / 0 FAIL / 0 ERROR / 2 SKIP.
- **Mutação: 11 de 11 detectados na 1ª rodada**, cada um pelo teste esperado.
- **Transparência:** uma asserção fraca que eu escrevi (retry idêntico conferindo só o status) foi reforçada **antes** da mutação, para provar que não há cobrança dupla.

## Pendências
- **Internas:** nenhuma identificada, fora a validação do navegador com Chromium real.
- **Exclusivas do Owner:** as anteriores; FX **real** para reconciliar histórico em moeda retirada; valor final comprovado para eventos legados sem snapshot; `correlation_id` nas integrações que entregarem resultados assincronamente.
- **Exclusivas de serviços externos:** as 12 integrações, incluindo Chromium/Playwright real; nenhuma conectada.
