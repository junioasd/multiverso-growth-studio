# CORREÇÃO CIRÚRGICA — AUDITORIA 1.0.10 → 1.0.11-OPERATIONAL-CANDIDATE

- **Auditoria:** `AUDITORIA_CIRURGICA_READINESS_OPERATIONAL_1_0_10_2026-09-25.md` (nesta pasta), com veredito **NÃO APROVADA — 1 bloqueador P1** (identidade/idempotência end-to-end).
- **Predecessores (inalterados):** 1.0.10 `f78ee197…16ec`, 1.0.9 … 1.0.1, v1.0.0 `932292a9…e42fe`.
- **Escopo:** estritamente o P1. Nenhuma frente nova, nenhuma refatoração ampla.
- **Status:** CANDIDATE — pronta para a auditoria cirúrgica final.

## Classificação da evidência
- **EXECUTADO_REALMENTE:**
  - caminho real `HttpGenerationProvider → run_metered_call → egress.urlopen → socket loopback` (o servidor recebeu **1** requisição para 2 chamadas);
  - SQLite real; threads reais (8 execuções e 6 redeliveries simultâneas);
  - reinício da guarda.
- **TESTADO_LOCALMENTE:** regressão completa e mutação.
- **SIMULADO:** `perform()` instrumentado (contador e bloqueio controlado para a execução "em curso").
- **NÃO_VERIFICADO / DEPENDENTE_DE_AMBIENTE_EXTERNO:** provedores pagos reais, plataformas, gateways, Chromium/Playwright real e FX real.

## Resultado
| | 1.0.10 (auditada) | 1.0.11 |
|---|---|---|
| Testes | 2.215 | 2.229 (+14) |
| PASS / FAIL / ERROR / SKIP | 2.213 / 0 / 0 / 2 | 2.227 / 0 / 0 / 2 |
| A: `run_metered_call` 2× com o mesmo `correlation_id` | **perform 2×; 2 reservas (correlation NULL); USD 2** | perform 1×; 1 reserva (`logical-1`); USD 1; retry `ALREADY_COMPLETED` |
| B: o mesmo `usage_event_id` após uma nova reserva | **liquidou A e B; USD 2; id não gravado** | devolve o resultado anterior; B intacta; USD 1; id gravado |

**Evidência:** `reproducao_bloqueador_cirurgico_1.0.10.py`, com saídas `reproducao_ANTES_1.0.10.txt` e `reproducao_DEPOIS_1.0.11.txt`.

## Causa-raiz do P1
As identidades foram implementadas **nos componentes**, mas **não conectadas ao caminho real**:
1. `run_metered_call` não repassava `params["correlation_id"]` para `reserve()`.
2. Em `record_usage`, o matching heurístico acontecia **antes** do `usage_event_id`, que só era gravado no ramo sem reserva.

## Contrato final de identidade end-to-end
**Origem → reserva → I/O → liquidação → uso:**
- a chamada lógica leva `correlation_id` (em `params`), vinculado ao fingerprint do payload **na reserva, antes do I/O**;
- a entrega do resultado leva `usage_event_id`, gravado **no próprio lançamento** (com ou sem reserva) sob índice único.

**Garantia:** uma identidade gera **no máximo uma** execução externa e **no máximo um** lançamento.

## Como o `correlation_id` atravessa `run_metered_call`
| Resposta de `reserve(correlation_id=…)` | O que acontece |
|---|---|
| nova reserva | `perform()` executa **uma vez**; a liquidação usa o `reservation_id` exato |
| `ALREADY_RESERVED` (reserva aberta, payload idêntico) | `perform()` **não** executa; pode haver uma execução em curso (ou órfã, que o Owner reconcilia) — política *at-most-once* |
| `ALREADY_COMPLETED` (terminal) | resposta terminal, **sem I/O** |
| `CORRELATION_ID_CONFLICT` (payload divergente) | recusa **antes** de qualquer I/O (antes era mascarado como "guarda indisponível") |

Concorrência: a reserva roda em `BEGIN IMMEDIATE` com índice único, então 8 chamadas simultâneas resultam em exatamente 1 execução. A garantia se mantém após reinício.

## Como o `usage_event_id` é persistido em uso ligado à reserva
- `settle(…, usage_event_id, usage_event_fingerprint)` grava ambos **no lançamento da liquidação**, dentro da mesma transação, sob o índice único `ux_ai_api_usage_event`.
- Numa corrida entre duas entregas concorrentes, o índice único decide: a perdedora recebe `IntegrityError`, que é tratado devolvendo o resultado anterior.
- A 1ª entrega e o retry devolvem o **mesmo** `usage_id`.

## Ordem final do matching
1. `usage_event_id` já conhecido → devolve o resultado anterior. Payload divergente, ou `reservation_id`/`correlation_id` apontando para outra reserva, gera `USAGE_EVENT_CONFLICT` **sem alterar nada**.
2. `reservation_id` explícito → valida a identidade persistida e usa **só** essa reserva.
3. `correlation_id` explícito → usa **só** a reserva desse id.
4. Sem identidade explícita: 0 candidatos = uso sem reserva (exige `usage_event_id`); 1 = inequívoco (o `usage_event_id` fica gravado, e redeliveries caem no passo 1); 2+ = `AMBIGUOUS_RESERVATION_MATCH`.

## Arquivos alterados
- **Produção:** `core/operations/cost_guard.py` (`run_metered_call`, `settle`, `record_usage` e o auxiliar `_usage_event_result`). Nenhuma mudança de esquema: as colunas e o índice já existiam desde a 1.0.10.
- **Testes:** novo `tests/test_operacional_1_0_11.py` (14 testes, itens 1–24). **Nenhum teste existente foi alterado.**

## Testes e mutação
- **Suítes:** 1.0.1–1.0.11, correções de auditoria, replay legado e verificador de conexões verdes; runner estrito RC 0.
- **Regressão completa:** 2.229 / 2.227 PASS / 0 FAIL / 0 ERROR / 2 SKIP.
- **Mutação: 7 de 7 detectados, cada um pelo teste esperado:**
  - `correlation_id` não propagado;
  - `ALREADY_*` reexecutando `perform`;
  - conflito mascarado como "guarda indisponível";
  - evento resolvido depois do matching;
  - `usage_event_id` não gravado na liquidação;
  - evento apontando outra reserva;
  - payload divergente aceito.
- **Transparência:**
  - um teste meu criava a reserva `b` **depois** de um replay divergente, que corretamente bloqueia o provedor; corrigi a ordem;
  - um mutante tinha o alvo com uma barra a mais e foi refeito.

## Bloqueadores P0/P1 internos restantes
Nenhum identificado. A validação do navegador com Chromium real permanece dependente de ambiente.

## Itens exclusivamente externos
- As 12 integrações, nenhuma conectada.
- Chromium/Playwright real.
- FX real.
- **Toda integração futura precisa enviar `correlation_id` e `usage_event_id` desde a origem.**
