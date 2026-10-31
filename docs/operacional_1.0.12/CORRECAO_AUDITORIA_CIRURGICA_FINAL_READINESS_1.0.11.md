# MICROCORREÇÃO CIRÚRGICA FINAL — AUDITORIA 1.0.11 → 1.0.12-OPERATIONAL-CANDIDATE

- **Auditoria:** `AUDITORIA_CIRURGICA_FINAL_READINESS_OPERATIONAL_1_0_11_2026-09-25.md` (nesta pasta), com veredito **NÃO APROVADA — 1 P1**: o fingerprint do `correlation_id` não cobria o payload operacional.
- **Predecessores (inalterados):** 1.0.11 `0564f0d5…d75e`, 1.0.10 … 1.0.1, v1.0.0 `932292a9…e42fe`.
- **Escopo:** estritamente o P1. Uma única função de produção alterada no fluxo, mais o módulo de canonicalização.
- **Status:** CANDIDATE — pronta para verificação cirúrgica final.

## Classificação da evidência
- **EXECUTADO_REALMENTE:** caminho `HttpGenerationProvider → run_metered_call → egress.urlopen → socket loopback` (o servidor recebeu **somente** o prompt A); SQLite real; threads reais (8 idênticas; 6 divergentes); reinício da guarda.
- **TESTADO_LOCALMENTE:** canonicalização (unitário), regressão completa e mutação.
- **SIMULADO:** `perform()` instrumentado nos testes de contagem; reserva legada com fingerprint antigo inserida via SQL.
- **NÃO_VERIFICADO / DEPENDENTE_DE_AMBIENTE_EXTERNO:** provedores pagos reais, plataformas, gateways, Chromium/Playwright real e FX real.

## Resultado
| | 1.0.11 (auditada) | 1.0.12 |
|---|---|---|
| Testes | 2.229 | 2.245 (+16) |
| PASS / FAIL / ERROR / SKIP | 2.227 / 0 / 0 / 2 | 2.243 / 0 / 0 / 2 |
| mesmo `correlation_id`, prompt diferente | **`ALREADY_COMPLETED`** (tratado como retry) | `CORRELATION_ID_CONFLICT` antes do I/O |
| fingerprint persistido | só metadados financeiros | metadados + **SHA-256 do payload operacional canônico** |

- **Evidência:** `reproducao_bloqueador_cirurgico_final_1.0.11.py`, com saídas `reproducao_ANTES_1.0.11.txt` e `reproducao_DEPOIS_1.0.12.txt`.
- **Leitura:** nas duas versões o prompt B **não** foi executado; a diferença é a classificação. Antes, uma operação **diferente** era mascarada como retry; agora o reuso indevido do identificador é exposto como conflito.

## Causa-raiz
- O fingerprint era calculado em `reserve()`, que só recebe metadados financeiros e de escopo.
- `run_metered_call()` tinha o payload operacional completo em `params`, mas não o vinculava à identidade.
- **Relato corrigido:** na 1.0.10 eu havia descrito esse fingerprint como "payload completo", e ele não era.

## Definição final do payload canônico
- **Documento:** `{"v": 1, "provider", "service", "operation", "params": <params exceto os campos excluídos>}`.
- **Campos incluídos:** provider, service e operation, mais **todos** os campos de `params`, exatamente como são enviados ao provedor. Isso cobre prompt, query, conteúdo, modelo, parâmetros de geração, opções do provedor, `estimated_cost`, `currency`, `project`, `campaign`, `job_id` e qualquer campo futuro.
- **Campos excluídos (e só estes):**
  - `correlation_id`: é a própria identidade da chamada lógica;
  - `usage_event_id`: é a identidade da **entrega** do resultado, não da requisição.
  - Nenhum campo de conteúdo é excluído; um campo novo entra automaticamente.

## Método de canonicalização
Payload canônico → JSON canônico (`sort_keys=True`, `separators=(",", ":")`, `ensure_ascii=False`, `allow_nan=False`) → **SHA-256** hexadecimal.

**Política de tipos (explícita e determinística):**
- `1`, `1.0`, `"1"` e `True` são **distintos**;
- `None` presente é **distinto** de campo ausente (conservador);
- a ordem das chaves **não** importa, inclusive em estruturas aninhadas;
- a ordem de listas **importa**; tupla é tratada como lista.

**Recusa fail-safe:** `set`, `bytes`, objetos, chaves não-texto e NaN/∞ geram `REQUEST_NOT_CANONICALIZABLE`, e a chamada é bloqueada **antes do I/O**. Nada é convertido por `str()`/`repr()`. O antigo `stable_fingerprint`, que usa `default=str`, **não** é usado para o payload operacional.

## Contrato final do `correlation_id`
**Identidade:** `stable_fingerprint("reservation_payload_v2", provider, service, operation, estimated_cost, currency, project, campaign, job_id, request_fingerprint)`, gravada na reserva **antes** de qualquer I/O.

| Situação | Resultado |
|---|---|
| mesmo id + mesmo payload canônico, reserva aberta | `ALREADY_RESERVED`, sem executar `perform()` |
| mesmo id + mesmo payload canônico, reserva concluída | `ALREADY_COMPLETED`, sem I/O |
| mesmo id + **qualquer** diferença semântica | `CORRELATION_ID_CONFLICT` **antes** do I/O |
| reserva legada sem fingerprint do payload | conflito "**não comprovável** (legado)", com revisão explícita |

- **Preservados sem mudança:** `reservation_id`, `usage_event_id`, `USAGE_EVENT_CONFLICT`, `RESERVATION_IDENTITY_MISMATCH`, a ordem de matching da 1.0.11, a concorrência (`BEGIN IMMEDIATE` + índice único) e o reinício.
- **Chamadas diretas a `authorize()` sem payload operacional:** mantêm o fingerprint de metadados. Uma reserva criada por `run_metered_call` só é reaproveitada por outra chamada com o mesmo payload.

## Arquivos alterados
- **Produção:** `core/operations/cost_guard.py`:
  - novos `_canonical`, `canonical_json`, `request_fingerprint` e `FINGERPRINT_EXCLUDED_FIELDS`;
  - `reserve()` aceita `request_fingerprint`;
  - `run_metered_call()` calcula e repassa o fingerprint.
  - Sem mudança de esquema.
- **Testes:** novo `tests/test_operacional_1_0_12.py` (16 testes, itens 1–23). **Nenhum teste existente foi alterado.**

## Testes e mutação
- **Suítes:** 1.0.1–1.0.12, correções de auditoria, replay legado e verificador de conexões verdes; runner estrito RC 0.
- **Regressão completa:** 2.245 / 2.243 PASS / 0 FAIL / 0 ERROR / 2 SKIP.
- **Mutação: 7 de 7 detectados na 1ª rodada, cada um pelo teste esperado:**
  - payload fora do fingerprint (o defeito original);
  - ordem de chaves relevante;
  - tipos ambíguos convertidos por `str()`;
  - `1.0` igualado a `1`;
  - exclusão ampla de campo de conteúdo;
  - payload não canonicalizável executando;
  - legado aceito como comprovável.

## Bloqueadores P0/P1 internos restantes
Nenhum identificado. A validação do navegador com Chromium real permanece dependente de ambiente.

## Itens exclusivamente externos
- As 12 integrações, nenhuma conectada.
- Chromium/Playwright real.
- FX real.
- **Toda integração futura precisa enviar `correlation_id` e `usage_event_id` desde a origem, e jamais reutilizar um `correlation_id` para uma operação diferente.**
