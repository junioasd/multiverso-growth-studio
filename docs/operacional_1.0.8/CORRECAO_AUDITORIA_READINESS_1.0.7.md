# CORREÇÃO DA AUDITORIA DE READINESS 1.0.7 → 1.0.8-OPERATIONAL-CANDIDATE

- **Auditoria:** `AUDITORIA_INDEPENDENTE_READINESS_OPERATIONAL_1_0_7_2026-09-25.md` (nesta pasta), com veredito **NÃO APROVADA** e 4 bloqueadores.
- **Predecessores (inalterados):** 1.0.7 `123dd1a9…c035`, 1.0.6 … 1.0.1, v1.0.0 `932292a9…e42fe`.
- **Status:** CANDIDATE — nova auditoria independente de readiness necessária.

## Classificação da evidência
- **EXECUTADO_REALMENTE:** SQLite real, threads reais, *triggers* abortando transações reais, backfill de upgrade via `init_db` real.
- **TESTADO_LOCALMENTE:** regressão completa e mutação.
- **NÃO_VERIFICADO / DEPENDENTE_DE_AMBIENTE_EXTERNO:** provedores pagos reais, gateways, plataformas, Chromium/Playwright real e cotações FX reais. As taxas dos testes são valores de teste; **nenhum custo, FX, venda ou publicação real** foi observado.

## Resultado
| | 1.0.7 (auditada) | 1.0.8 |
|---|---|---|
| Testes | 2.162 | 2.181 (+19) |
| PASS / FAIL / ERROR / SKIP | 2.160 / 0 / 0 / 2 | 2.179 / 0 / 0 / 2 |
| B1 `ZZZ` | **aceito, persistido e autorizado** | `UNSUPPORTED_CURRENCY` / `INVALID_CURRENCY` |
| B2 migração 10¹² × 10¹² | **limite de 10²⁴ gravado e usado** | `FX_CONVERSION_OUT_OF_RANGE`; nada alterado |
| B3 write-off de `SETTLEMENT_STATE_CONFLICT` | **reserva presa em `HELD_CONFLICT`**; migração bloqueada | reserva `WRITTEN_OFF`; migração liberada |
| B4 `record_usage` sem custo | **bloqueio sem objeto reconciliável** | incidente vinculado + `reconcile_usage` |

- **Evidência:** `reproducao_bloqueadores_readiness_1.0.7.py`, com saídas `reproducao_ANTES_1.0.7.txt` e `reproducao_DEPOIS_1.0.8.txt`.
- **Leitura da linha "open_reservations lista? False" (B3):** no ANTES, a reserva estava **pendente e escondida** (`HELD_CONFLICT` fora da inspeção); no DEPOIS, ela foi **encerrada** (`WRITTEN_OFF`).

## Causas-raiz
1. **B1:** `valid_currency` só conferia o **formato** (3 letras maiúsculas).
2. **B2:** a migração validava a taxa, mas **não os valores resultantes**; os *triggers* só barravam acima de 10³⁰⁸.
3. **B3:** o write-off encerrava o incidente sem tocar na reserva, e `open_reservations()` ignorava `HELD_CONFLICT`.
4. **B4:** `record_usage()` gravava cobrança nula sem criar nenhum objeto reconciliável.

## Políticas finais
- **Moedas suportadas:** lista oficial **BRL, USD, EUR, GBP**.
  - O Owner pode ampliar via `MGS_EXTRA_CURRENCIES` **somente** com códigos ISO 4217 reais de uma lista embutida (JPY, CAD, AUD, CHF, MXN, ARS, CLP, COP, PEN, UYU, PYG, BOB, CNY, INR, NZD, SEK, NOK, DKK); código fora dela é ignorado, e nenhuma moeda é inventada.
  - Validada em limite, reserva, liquidação (moeda reportada inválida vira incidente), uso, reconciliação e migração, e **antes** de qualquer outra checagem.
  - Moeda legada fora da política falha fechada; `retire_unsupported_currency_limits` (Owner, evidência) remove limites legados **somente** sem histórico financeiro nessa moeda, e caso contrário exige reconciliação.
- **`MAX_MONEY` (10¹²):**
  - na migração, **todo valor resultante** (limites e lançamentos materializados) precisa ser finito, estar em [0, 10¹²] e **não sofrer underflow** (valor positivo não pode virar 0); qualquer violação aborta a transação inteira;
  - *triggers* no banco com o teto da política cobrem limites, reservas, custo, cobrança e valor convertido;
  - limite legado fora da faixa **nunca governa** (`MONETARY_LIMIT_EXCEEDED`);
  - `adjustments` na reconciliação exigem |valor| ≤ 10¹².
- **Semântica de write-off:** quando ligado a uma reserva pendente (`RESERVED`, `HELD_UNKNOWN` ou `HELD_CONFLICT`), incidente, reserva e ledger são encerrados **no mesmo commit**.
  - A reserva vai para o estado terminal **`WRITTEN_OFF`**, e a **cobrança conservadora é mantida** (reserva nunca liquidada vira cobrança pelo valor reservado).
  - Retry idêntico é idempotente; divergente gera `RECONCILIATION_CONFLICT`.
  - `open_reservations()` lista exatamente os estados pendentes, `HELD_CONFLICT` incluído.
- **Reconciliação de uso sem custo:** `record_usage()` sem custo cria, **na mesma transação**, o incidente `USAGE_COST_UNKNOWN` vinculado ao `usage_id` (identidade persistente).
  - O provedor fica bloqueado; `reconcile_usage()` (atômico e idempotente, pelo fluxo de incidentes) aplica o valor final comprovado na moeda operacional.
  - Write-off exige o **teto confiável** do provedor, nunca custo presumido zero.
  - **Upgrade:** `init_db` cria, de forma idempotente, incidentes para usos órfãos de versões anteriores. Não existe `COST_RECONCILIATION_REQUIRED` sem objeto reconciliável.

## Revisão da classe — variantes encontradas e corrigidas
- **V1:** `preview` com moeda inválida num provedor sem limites reportava `PAID_PROVIDER_NOT_AUTHORIZED` (fechado, mas com o motivo errado). A política de moeda passou para o início da decisão.
- **V2:** `adjustments` da reconciliação FX não tinham teto monetário. Agora têm.
- **V3:** limite legado fora da faixa (gravado antes dos *triggers* novos) governaria o orçamento. Agora bloqueia.
- **V4:** write-off de reserva **nunca liquidada** (`SETTLEMENT_FAILED`) sumiria do ledger. Agora vira cobrança pelo valor reservado.

## Arquivos
- **Produção:**
  - `core/operations/cost_guard.py`
  - `core/models/db.py` (coluna `usage_id` nos incidentes, backfill de upgrade idempotente, *triggers* `trg_money_v108_*`; tudo aditivo)
- **Testes:** novo `tests/test_operacional_1_0_8.py` (19 testes, itens 1–37 e variantes). **Nenhum teste existente foi alterado.**

## Testes e mutação
- **Suítes:** 1.0.1–1.0.8, correções de auditoria e replay legado verdes; runner estrito RC 0.
- **Regressão completa:** 2.181 / 2.179 PASS / 0 FAIL / 0 ERROR / 2 SKIP.
- **Mutação: 12 de 12 detectados na 1ª rodada, cada um pelo teste esperado.**

## Pendências
- **Internas:** nenhuma identificada, fora a validação do navegador com Chromium real.
- **Exclusivas do Owner:** as anteriores; a lista de moedas extras (se desejar), a revisão de moedas legadas e a reconciliação de usos sem custo.
- **Exclusivas de serviços externos:** as 12 integrações, incluindo Chromium/Playwright real; nenhuma conectada.
