# CORREÇÃO DA AUDITORIA DE READINESS 1.0.4 → 1.0.5-OPERATIONAL-CANDIDATE

- **Auditoria:** `AUDITORIA_INDEPENDENTE_READINESS_OPERATIONAL_1_0_4_2026-09-25.md` (nesta pasta), com veredito **NÃO APROVADA**: 4 bloqueadores e 1 observação estrutural.
- **Predecessores (inalterados):** 1.0.4 `b49238a0…c72`, 1.0.3, 1.0.2, 1.0.1, v1.0.0 `932292a9…e42fe`.
- **Status:** CANDIDATE — nova auditoria independente de readiness necessária.

## Resultado
| | 1.0.4 (auditada) | 1.0.5 |
|---|---|---|
| Testes | 2.113 | 2.126 (+13) |
| PASS / FAIL / ERROR / SKIP | 2.111 / 0 / 0 / 2 | 2.124 / 0 / 0 / 2 |
| B1 `run_server()` sem main.py | modo biblioteca; **busca paga executou** | camada operacional ativa; bloqueada |
| B2 limite 0, estimativa 0, sem custo | **busca e geração executaram**, nada cobrado | `INVALID_COST`; 0 requisições |
| B3 incidente de moeda encerrado com texto | **aceito; provedor liberado** | recusado; exige reconciliação |
| B4 settle repetido com USD 100 | **`already_settled`** | `SettlementConflict` + incidente |
| Navegador: trava durante o `goto` | **`evaluate` executou** | `evaluate` não roda |

**Evidência:** `reproducao_bloqueadores_readiness_1.0.4.py`, com saídas `reproducao_ANTES_1.0.4.txt` e `reproducao_DEPOIS_1.0.5.txt` (mesmo script; o DEPOIS roda numa cópia descartável porque `run_server()` sem app cria o banco padrão no diretório corrente).

## Causas-raiz e correções
1. **B1 — a configuração operacional só existia em `main.py`.**
   - Nova configuração **única**: `core/operations/runtime.py::configure_operational`.
   - `api/server.py::run_server` a chama **quando constrói a própria aplicação**, exatamente como a auditoria pediu. Quem passa o próprio `app` (o `main.py`, que já configurou, ou quem embute o servidor) não tem o estado do processo alterado.
   - Um **teste estático** exige que todo entrypoint de produção faça isso. Ele encontrou um **terceiro caso real**, o comando `main.py health`, também corrigido.
2. **B2 — a estimativa do chamador valia como reserva, e custo desconhecido sem teto não gerava incidente.** Nova política:
   - chamada paga exige estimativa **> 0**;
   - novo limite do Owner `unknown_cost_upper_bound` (teto **confiável** por chamada): a reserva passa a ser, no mínimo, esse teto, e não a estimativa arbitrária do chamador;
   - executado com custo desconhecido **sem** esse teto abre `COST_UNKNOWN_NO_UPPER_BOUND` e bloqueia o provedor.
3. **B3 — `resolve_incident` só trocava o status.**
   - Incidentes financeiros (overrun, custo inválido, moeda, falha de liquidação, custo desconhecido sem teto, replay divergente) **só saem por `reconcile_incident`**.
   - A reconciliação exige Owner, evidência, fonte e valor final comprovado **na moeda do orçamento** (quando a moeda original é outra, também `fx_rate`, `fx_source` e `fx_timestamp`), ou uma decisão explícita de write-off.
   - O ledger é corrigido **antes** de liberar o provedor. Reserva nunca liquidada é liquidada pelo valor final.
   - Tentar encerrar só por status gera `ValueError`.
4. **B4 — a liquidação não guardava o conteúdo.**
   - Fingerprint persistido (`settlement_fingerprint`): conteúdo igual é retry idempotente; diferente gera `SettlementConflict`, abre `SETTLEMENT_REPLAY_CONFLICT` e bloqueia o provedor.
   - Liquidação legada sem fingerprint é tratada como **não comprovável** e cai em conflito (fail-safe).
   - **Defeito meu encontrado aqui:** o `ROLLBACK` sem transação ativa **mascarava** o conflito como `OperationalError`. Agora só há rollback com transação aberta.
5. **Navegador:** checagem do kill switch imediatamente antes de `page.evaluate` (a ação mutável). **Validação com Chromium real continua pendente**, porque o Chromium não está instalado neste ambiente.

## Arquivos
- **Produção:**
  - novo `core/operations/runtime.py`
  - `api/server.py`
  - `main.py`
  - `core/operations/cost_guard.py`
  - `bridge/execution/browser_executor.py`
  - `core/models/db.py` (colunas aditivas: `settlement_fingerprint` e campos de reconciliação)
- **Testes:** novo `tests/test_operacional_1_0_5.py` (13 testes).
- **Testes existentes ajustados** (4, consequência direta e exigida pelas novas políticas; nenhum removido):
  - `test_operacional_1_0_2::test_1` e `::test_2`: o provedor pago sem custo informado agora exige o teto confiável do Owner (B2). O setup configura `unknown_cost_upper_bound` com valores que mantêm **as mesmas reservas e asserções**.
  - `test_operacional_1_0_3::test_negative_cost…` e `test_operacional_1_0_4::test_16_17_18…`: `resolve_incident` passou a `reconcile_incident` (B3). O teste de moeda ficou **mais estrito**: exige que a reconciliação tenha efeito financeiro real (USD 108 reconhecidos acima do limite de USD 10 geram `LIMIT_EXCEEDED`).

## Testes e mutação
- **Suítes:** 1.0.1–1.0.5 verdes; runner estrito RC 0.
- **Regressão completa:** 2.126 / 2.124 PASS / 0 FAIL / 0 ERROR / 2 SKIP.
- **Mutação: 13 de 13 detectados, cada um pelo teste esperado:**
  - `run_server` sem a camada operacional; runtime não marcado; `health` sem a camada;
  - estimativa zero; custo desconhecido sem incidente; reserva ignorando o teto;
  - resolução só por status; reconciliação sem FX; reconciliação sem corrigir o ledger;
  - replay divergente aceito; legado aceito;
  - `evaluate` sem checagem;
  - `ROLLBACK` mascarando o conflito.

## Defeitos meus nesta rodada (transparência)
- **1ª regressão completa com 58 falhas.** `run_server` passou a marcar o processo como operacional **mesmo quando recebia o próprio app**, e quatro módulos de teste que embutem o servidor vazaram esse estado global. Corrigido conforme o texto da auditoria ("quando construir a própria aplicação"), com um teste que proíbe o vazamento.
- Erro de sintaxe no 1º patch da guarda de custo (não aplicado; refeito).
- `ROLLBACK` mascarando exceção (acima).
- A reprodução do B1 criou `./data/mgs.db` na árvore; removido, e a evidência passou a rodar numa cópia descartável.

## Pendências
- **Internas:** nenhuma identificada, fora a validação do navegador com Chromium real.
- **Exclusivas do Owner:** as anteriores, mais **definir o `unknown_cost_upper_bound` de cada provedor pago** (sem ele, custo desconhecido bloqueia o provedor por desenho).
- **Exclusivas de serviços externos:** as 12 integrações, incluindo Chromium/Playwright real; nenhuma conectada.
