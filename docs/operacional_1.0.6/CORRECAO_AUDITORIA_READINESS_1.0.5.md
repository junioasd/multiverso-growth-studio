# CORREÇÃO DA AUDITORIA DE READINESS 1.0.5 → 1.0.6-OPERATIONAL-CANDIDATE

- **Auditoria:** `AUDITORIA_INDEPENDENTE_READINESS_OPERATIONAL_1_0_5_2026-09-25.md` (nesta pasta), com veredito **NÃO APROVADA** e 4 bloqueadores.
- **Predecessores (inalterados):** 1.0.5 `0b1476fb…2e00`, 1.0.4, 1.0.3, 1.0.2, 1.0.1, v1.0.0 `932292a9…e42fe`.
- **Status:** CANDIDATE — nova auditoria independente de readiness necessária.

## Resultado
| | 1.0.5 (auditada) | 1.0.6 |
|---|---|---|
| Testes | 2.126 | 2.145 (+19) |
| PASS / FAIL / ERROR / SKIP | 2.124 / 0 / 0 / 2 | 2.143 / 0 / 0 / 2 |
| B1 teto = 0 | **aceito; 2 chamadas sem custo, sem incidente** | rejeitado; teto 0 legado inválido no uso; 1 chamada e incidente |
| B2 duas reconciliações concorrentes | **ambas `RECONCILED`** (última gravação vence) | uma vence; a outra `RECONCILIATION_CONFLICT` |
| B3 USD → EUR com histórico em USD | **aceito; USD somado como EUR** | `BUDGET_CURRENCY_CHANGE_REQUIRES_RECONCILIATION` |
| B4 `NOT_EXECUTED`/`UNKNOWN` com custo 100 | **liberado / cobrado 1** | custo 100 preservado; `SETTLEMENT_STATE_CONFLICT` e bloqueio |

- **Evidência:** `reproducao_bloqueadores_readiness_1.0.5.py`, com saídas `reproducao_ANTES_1.0.5.txt` e `reproducao_DEPOIS_1.0.6.txt` (mesmo script).
- **Nota:** o rótulo "(os 50 USD contados como EUR?)" na linha B3 é uma pergunta fixa do script. No DEPOIS, a resposta é `CURRENCY_MISMATCH`: nada é somado.

## Causas-raiz e correções
1. **B1 — o teto era validado só como "≥ 0" e considerado válido no uso só por existir.**
   - `unknown_cost_upper_bound` exige número real finito **estritamente > 0**, tanto no `set_limit` quanto **no uso**. Um teto 0 já gravado por versão anterior conta como "sem teto válido".
   - Custo desconhecido sem teto válido abre incidente (`COST_UNKNOWN_NO_UPPER_BOUND`, equivalente ao `COST_UNKNOWN_NO_VALID_UPPER_BOUND` pedido) e bloqueia o provedor; o custo nunca é presumido zero.
2. **B2 — a reconciliação lia, alterava o ledger e fechava o incidente em transações separadas.**
   - `reconcile_incident` roda em **uma transação `BEGIN IMMEDIATE`**: reivindica o incidente, corrige o ledger e grava a resolução, com `UPDATE … WHERE status = OPEN` e fingerprint persistido.
   - Retry idêntico é idempotente; divergente, ou original sem fingerprint, gera `RECONCILIATION_CONFLICT`. A 1ª reconciliação é sempre preservada.
   - Crash no meio da transação: ledger e status são desfeitos **juntos** (teste com *trigger* que aborta a gravação do incidente).
3. **B3 — `_committed` somava sem filtrar a moeda, e `set_limit` trocava a moeda livremente.**
   - Todo cálculo de orçamento é **filtrado pela moeda**.
   - `set_limit` recusa moeda diferente de limites ou histórico existentes (`BUDGET_CURRENCY_CHANGE_REQUIRES_RECONCILIATION`).
   - Nova `migrate_budget_currency`: Owner, taxa FX > 0, fonte, timestamp e evidência; proibida com reservas ou incidentes abertos; registrada em `ai_api_currency_migrations`. O histórico **não é reescrito**: passa a entrar convertido pela taxa registrada, nunca 1:1.
   - Banco vindo da 1.0.5, com limites já trocados sem migração, é bloqueado: o histórico nunca é ignorado.
4. **B4 — em `settle()`, o rótulo do desfecho dominava o custo real informado.**
   - `NOT_EXECUTED` com custo real > 0 (ou com sucesso) e `UNKNOWN` com custo real conhecido abrem `SETTLEMENT_STATE_CONFLICT`.
   - A cobrança é `max(reserva, custo real)`, em estado `HELD_CONFLICT`: o orçamento **não é liberado** e o custo real **nunca é descartado**.
   - `NOT_EXECUTED` sem custo continua liberando normalmente.

## Revisão da classe inteira — variantes encontradas e corrigidas
- **V1:** `reconcile()` de reserva aceitava `executed=False` com custo real (mesma contradição do B4). Agora é rejeitado.
- **V2:** `reconcile()` de reserva em `HELD_UNKNOWN` atualizava sem condição de estado, então duas reconciliações concorrentes venceriam (mesma classe do B2). Agora usa transação atômica com reivindicação condicionada.
- **V3:** `reconcile()` de reserva já encerrada aceitava retry **divergente** como `already_settled`. **Encontrado pelo teste de variante.** Agora usa fingerprint: idêntico é idempotente, divergente é conflito.
- **V4:** `reconcile()` de reserva com custo **acima** do reservado não abria `COST_OVERRUN`. Agora abre, igual ao `settle`.
- **V5:** `settle(NOT_EXECUTED, success=True)` era aceito. Agora é `SETTLEMENT_STATE_CONFLICT`.
- **V6:** teto 0 legado já gravado era válido no uso. Agora é inválido.

## Arquivos
- **Produção:**
  - `core/operations/cost_guard.py`
  - `core/models/db.py` (coluna `reconciliation_fingerprint`; tabela `ai_api_currency_migrations` com `CHECK (fx_rate > 0)`; ambos aditivos)
- **Testes:** novo `tests/test_operacional_1_0_6.py` (19 testes, cobrindo os itens 1–27 e as variantes). **Nenhum teste existente foi alterado.**

## Testes e mutação
- **Suítes:** 1.0.1–1.0.6, correções de auditoria e replay legado verdes; runner estrito RC 0.
- **Regressão completa:** 2.145 / 2.143 PASS / 0 FAIL / 0 ERROR / 2 SKIP.
- **Mutação: 12 de 12 detectados, cada um pelo teste esperado.**
- **Transparência:** 2 mutantes passaram cegos na 1ª rodada:
  - "ledger e status em commits separados" estava **mal construído** (o commit extra ficava depois das duas escritas); refeito entre elas, foi detectado pelo teste de crash;
  - "histórico estrangeiro somado 1:1" revelou **lacuna real de cobertura** (o upgrade de banco da 1.0.5), fechada com teste novo.

## Pendências
- **Internas:** nenhuma identificada, fora a validação do navegador com Chromium real.
- **Exclusivas do Owner:** as anteriores; o teto confiável de cada provedor pago precisa ser **> 0**; e, se quiser trocar a moeda de um orçamento, usar `migrate_budget_currency` com FX real.
- **Exclusivas de serviços externos:** as 12 integrações, incluindo Chromium/Playwright real; nenhuma conectada.
