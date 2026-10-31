# CORREÇÃO DA AUDITORIA DE READINESS 1.0.9 → 1.0.10-OPERATIONAL-CANDIDATE

- **Auditoria:** `AUDITORIA_INDEPENDENTE_READINESS_OPERATIONAL_1_0_9_2026-09-25.md` (nesta pasta), com veredito **NÃO APROVADA**: 4 bloqueadores, 1 variante e 1 observação de lifecycle.
- **Predecessores (inalterados):** 1.0.9 `05e6d7ef…3de4`, 1.0.8 … 1.0.1, v1.0.0 `932292a9…e42fe`.
- **Status:** CANDIDATE — nova auditoria independente de readiness necessária.

## Classificação da evidência
- **EXECUTADO_REALMENTE:** SQLite real, threads reais (8 entregas simultâneas, 6 com o mesmo `usage_event_id`), reinícios com nova instância do `CostGuard`, verificador estático de conexões.
- **TESTADO_LOCALMENTE:** regressão completa e mutação.
- **SIMULADO:** estados legados (limites em ZZZ, reservas sem fingerprint) inseridos via SQL, e um incidente marcado como reconciliado via SQL no teste de retirada pós-reconciliação.
- **NÃO_VERIFICADO / DEPENDENTE_DE_AMBIENTE_EXTERNO:** provedores pagos reais, gateways, plataformas, Chromium/Playwright real e FX real. Nenhum custo, venda, publicação ou câmbio real foi observado.

## Resultado
| | 1.0.9 (auditada) | 1.0.10 |
|---|---|---|
| Testes | 2.197 | 2.215 (+18) |
| PASS / FAIL / ERROR / SKIP | 2.195 / 0 / 0 / 2 | 2.213 / 0 / 0 / 2 |
| B1 `cid` + USD 500 + outro project | **`AUTHORIZED` (reaproveitou USD 1)** | `CORRELATION_ID_CONFLICT` |
| B2 `reservation_id` com identidade errada | **liquidado** | `RESERVATION_IDENTITY_MISMATCH`; nada alterado |
| B3 o mesmo uso 2× | **2 usos, 2 incidentes, USD 200** | sem id: recusado; com id: 1 uso, USD 100 |
| B4 retirada com `snapshot_currency=ZZZ` | **limite removido; ciclo sem saída** | recusada; saída pela recuperação legada |
| B5 `reconcile_usage` em ZZZ | **lançamento em ZZZ** | `UNSUPPORTED_CURRENCY`; nada alterado |
| Lifecycle: `COMMITTED` + retry | **`AUTHORIZED`** | `ALREADY_COMPLETED` (`authorized=False`) |

**Evidência:** `reproducao_bloqueadores_readiness_1.0.9.py`, com saídas `reproducao_ANTES_1.0.9.txt` e `reproducao_DEPOIS_1.0.10.txt`.

## Causas-raiz
1. **B1:** o retry por `correlation_id` comparava só provider/service/operation.
2. **B2:** `record_usage` liquidava por `reservation_id` sem conferir a identidade persistida.
3. **B3:** o uso sem reserva não tinha identidade do evento.
4. **B4:** a retirada conferia apenas `currency` do incidente, não `snapshot_currency`, `original_currency` ou `final_currency`.
5. **B5:** a reconciliação não validava a moeda final contra a política.
6. **Lifecycle:** nenhum estado terminal era informado no retry.

## Fingerprint final do `correlation_id`
- **Composição:** `stable_fingerprint("reservation_payload", provider, service, operation, repr(estimated_cost), currency, project, campaign, job_id)`, persistido em `correlation_fingerprint`.
- **Por que a estimativa e não o valor reservado:** `reserved_amount` é **derivado** (`max(estimativa, teto confiável)`); vincular a estimativa de entrada garante que o mesmo pedido gere a mesma identidade.
- **Legado:** reserva legada sem fingerprint é **não comprovável** e gera conflito.

## Lifecycle oficial do `correlation_id`
- reserva aberta + retry idêntico → `ALREADY_RESERVED` (`authorized=True`, a mesma reserva, sem reexecutar orçamento);
- reserva terminal (`COMMITTED`, `RELEASED`, `WRITTEN_OFF`) ou em conciliação (`HELD_*`) + retry idêntico → `ALREADY_COMPLETED` (`authorized=False`, com o status);
- payload divergente → `CORRELATION_ID_CONFLICT`.

## Identidade oficial dos usos sem reserva
- `usage_event_id` é **obrigatório** sem reserva (`USAGE_EVENT_ID_REQUIRED`), com índice único persistente.
- `usage_event_fingerprint` = `stable_fingerprint("usage_event", provider, service, operation, success, cost, currency, cost_confirmed, tokens, units, job_id, project, campaign)`.
- **Tudo em `BEGIN IMMEDIATE`:** retry idêntico devolve o **mesmo** `usage_id` e incidente (`already_recorded`); payload divergente gera `USAGE_EVENT_CONFLICT`.
- **Por que obrigatório e não opcional:** com identidade opcional, a reprodução exata da auditoria (duas chamadas sem id) continuaria duplicando. **Não há chamador de produção de `record_usage`**; integrações assíncronas futuras deverão trazer o id desde a origem.

## Regra oficial de validação reservation↔usage
- Por `reservation_id` ou `correlation_id`, a identidade **persistida** é a fonte da verdade.
- **Comparação obrigatória:** provider, service e operation. **Comparação quando informados:** project, campaign e job_id.
- Divergência gera `RESERVATION_IDENTITY_MISMATCH` ou `CORRELATION_IDENTITY_MISMATCH` **antes** de qualquer alteração.
- Sem identidade: 0 candidatos = uso sem reserva (exige `usage_event_id`); 1 = inequívoco; 2+ = `AMBIGUOUS_RESERVATION_MATCH`.

## Regra final de retirada de moeda
Uma moeda **não** é retirada se existir qualquer:
- uso com moeda **efetiva** nela;
- reserva nela;
- incidente **aberto** que a referencie em `currency`, `snapshot_currency`, `original_currency` ou `final_currency`.

Nesses casos o erro é `CURRENCY_RETIREMENT_REQUIRES_RECONCILIATION` e o limite é preservado.

**Caminho de saída (sem ciclo):** `reconcile_unsupported_currency_history` passou a **aceitar** incidentes `USAGE_COST_UNKNOWN` abertos (eles não têm valor a converter). Depois, `reconcile_usage` resolve o evento na moeda nova usando a **taxa registrada** da migração única `snapshot_currency→atual` (write-off = teto do evento × taxa, nunca 1:1).

## Variantes encontradas e corrigidas
- **V1 (ciclo sem saída):** ao bloquear a retirada (B4) e a reconciliação em ZZZ (B5), a recuperação legada ainda recusava incidentes abertos, e o Owner ficaria sem saída. Resolvido como descrito acima.
- **V2:** o write-off de um evento cuja moeda foi depois migrada usaria o teto 1:1. Agora converte pela taxa registrada.
- **V3:** `reconcile()` de reserva em moeda não suportada criaria um lançamento novo nessa moeda. Agora gera `UNSUPPORTED_CURRENCY`.
- **V4:** o write-off em orçamento de moeda não suportada também gravaria em ZZZ. Agora é recusado.
- **V5 (encontrada pela regressão completa):** o novo gerenciador de transação abria a conexão num método e fechava em outro, o que o verificador estático de conexões recusa. Reescrito como `contextmanager`, em que a mesma função abre e fecha; o teste antigo **não** foi afrouxado.

## Testes existentes ajustados (10 chamadas em 3 arquivos; documentado no cabeçalho de cada um)
- **Arquivos:** `test_operacional_1_0_1.py` (1), `test_operacional_1_0_8.py` (4) e `test_operacional_1_0_9.py` (5).
- **Expectativa antiga:** registrar uso sem reserva **sem identidade**, o que era o próprio defeito B3.
- **Nova expectativa:** cada chamada recebe um `usage_event_id` **distinto**.
- **Intenção preservada:** eventos distintos continuam distintos, e **nenhuma asserção mudou**.

## Arquivos
- **Produção:**
  - `core/operations/cost_guard.py`
  - `core/models/db.py` (colunas aditivas `correlation_fingerprint`, `usage_event_id` e `usage_event_fingerprint`, mais índice único parcial)
- **Testes:** novo `tests/test_operacional_1_0_10.py` (18 testes, itens 1–40) e os 3 ajustes acima.

## Testes e mutação
- **Suítes:** 1.0.1–1.0.10, correções de auditoria, replay legado e verificador de conexões verdes; runner estrito RC 0.
- **Regressão completa:** 2.215 / 2.213 PASS / 0 FAIL / 0 ERROR / 2 SKIP. A 1ª execução acusou V5; a 2ª, após a correção, ficou verde.
- **Mutação: 12 de 12 detectados, cada um pelo teste esperado.** O mutante de concorrência foi refeito sobre o código final e também foi detectado.

## Pendências
- **Internas:** nenhuma identificada, fora a validação do navegador com Chromium real.
- **Exclusivas do Owner:** as anteriores; FX real para moedas retiradas; e a obrigação de **enviar `usage_event_id`/`correlation_id` desde a origem** em qualquer integração futura que registre uso.
- **Exclusivas de serviços externos:** as 12 integrações, incluindo Chromium/Playwright real; nenhuma conectada.
