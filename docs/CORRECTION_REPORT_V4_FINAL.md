# Relatório — Microcorreção Final: 2 Resíduos (EXPANSION V3 → V4)

Base: `MULTIVERSO_GROWTH_STUDIO_MASTER_V1_EXPANSION_V3_MICROCORRECTION`
(825 testes). Esta rodada corrige exatamente os 2 resíduos apontados,
sem implementar nenhuma expansão nova.

## Contagem de testes — extraída da execução real

```
Ran 844 tests
OK
```

**844 testes, 844 PASS, 0 FAIL.** 825 preservados (nenhum removido, nenhum
enfraquecido) + **19 testes novos** (`tests/test_microcorrection_v4.py`).
Suíte completa confirmada estável em 3 execuções consecutivas.

## Lista exata dos arquivos alterados

**Reescritos:**
- `navigator/models.py` — separa `execution_id`/`attempt_id`/
  `idempotency_key`/`critical_action_key`; novo `ExecutionResult.NEEDS_VERIFICATION`;
  nova `RETRY_SAFE_ACTIONS`; nova `compute_attempt_identity()`
- `navigator/execution_log.py` — persiste `execution_id`/`attempt_id`;
  novo `by_task()`
- `navigator/executor.py` — reescrito por completo: ações críticas usam
  `critical_action_key` (estável); ações não críticas usam identidade
  de tentativa (inclui `attempt_id`); gate de retry-safety; `NEEDS_VERIFICATION`
  para resultado incerto de ação crítica
- `core/reasoning/engine.py` — gera `execution_id`/`attempt_id` por
  rodada de replan, injetados na tarefa
- `core/reasoning/adapters.py` — `NavigatorTaskExecutor` propaga
  `execution_id`/`attempt_id`/`retry_safe`
- `forge/engineer.py` — `rollback()` reescrito: `ROLLED_BACK` só com
  restauração real e verificada; hash de checkpoint; validação de
  restorer
- `core/models/db.py` — colunas aditivas `execution_id`/`attempt_id`
  em `navigator_execution_log`

**Novo arquivo de teste:**
- `tests/test_microcorrection_v4.py` (19 testes)

## Ponto 1 — REPLAN + REEXECUTE com NAVIGATOR real ✅

**Causa raiz do bug**: a idempotência do NAVIGATOR usava uma única
identidade (mission_id+task_id+action+platform+payload) para TUDO —
tentativas de replan reutilizavam a mesma identidade e a idempotência
devolvia o `FAILED` anterior sem reexecutar.

**Correção**: separação de 6 conceitos —`mission_id`/`task_id`
(inalterados), `execution_id` (agrupa as tentativas de uma rodada),
`attempt_id` (identifica cada tentativa), `idempotency_key` (para
ações não críticas, inclui `attempt_id` — nova tentativa = nova
identidade = retry legítimo permitido; mesma tentativa repetida =
idempotente), `critical_action_key` (para ações críticas, SEMPRE
estável — SEM `attempt_id` — Owner Approval e consumo protegidos
contra duplicação não importa quantas tentativas/replans aconteçam).

Retry só é permitido quando a ação é `retry_safe` por padrão
(`RETRY_SAFE_ACTIONS`: leituras/checagens de status) ou o chamador
declara `retry_safe_override=True` explicitamente — caso contrário,
nova tentativa é recusada (`BLOCKED`). Resultado INCERTO de ação
crítica (exceção durante a consumação) vira `NEEDS_VERIFICATION` —
nunca repetido automaticamente.

**Validado manualmente todos os 4 cenários adversariais exigidos**:
(1) tentativa 1 FAILED / tentativa 2 SUCCESS → executor chamado 2x,
REPLAN registrado, `attempt_id` diferente, SUCCESS final; (2) mesma
`attempt_id` repetida → executor NUNCA chamado de novo, mesmo
resultado; (3) ação crítica com resultado incerto →
`NEEDS_VERIFICATION`, nunca repetida automaticamente mesmo com novo
`attempt_id`; (4) restart entre tentativas → estado preservado, retry
legítimo continua funcionando. Também validados: ação não retry-safe
bloqueia nova tentativa sem override explícito; ação crítica nunca
duplicada independente de quantos `attempt_id` diferentes forem
usados; cadeia completa `ReasoningEngine → REPLAN → NAVIGATOR →
IDEMPOTENCY → EXECUTOR REAL` funcionando de ponta a ponta com o
cenário original do bug.

## Ponto 2 — FORGE: rollback só é ROLLED_BACK se restaurar de fato ✅

**Causa raiz do bug**: quando um `repair_backup` existia mas nenhum
`executor` era fornecido (ex: após restart sem reconectar o
resource_store), o código antigo caía no mesmo ramo "sem nada para
restaurar" e marcava `ROLLED_BACK` de qualquer forma — o recurso
continuava no estado incorreto.

**Correção**: fluxo obrigatório LOAD CHECKPOINT → VALIDATE RESTORER →
RESTORE → VERIFY RESTORATION → COMPARE HASH/STATE → RECORD EVIDENCE →
ROLLED_BACK. Cada etapa pode falhar e SEMPRE resulta em
`ROLLBACK_FAILED` (nunca `ROLLED_BACK`) quando falha:
- Checkpoint existe mas nenhum executor disponível → `ROLLBACK_FAILED`
  ("nenhum executor/restorer disponível").
- Hash do backup divergente do registrado no momento da correção
  (checkpoint corrompido) → `ROLLBACK_FAILED`, detectado ANTES de
  tocar o recurso.
- Executor não cobre todos os recursos do checkpoint → `ROLLBACK_FAILED`.
- Falha durante a restauração ou verificação pós-restauração →
  `ROLLBACK_FAILED`.
- Exceção: nenhum backup existe (nunca houve correção real aplicada)
  → `ROLLED_BACK` vacuamente correto (preserva o comportamento já
  aprovado para "checkpoint inexistente").

`find_blocked_operation` nunca trata `ROLLBACK_FAILED` como liberado —
a operação permanece bloqueada até uma restauração genuína e
verificada.

**Validado manualmente o cenário exato exigido**: original=A, fix=B,
restart, restorer indisponível, rollback solicitado →
`ROLLBACK_FAILED`, nunca `ROLLED_BACK`, `rollback_restored=False`,
recurso permanece B, operação continua bloqueada. Depois, conectando
um restorer válido e tentando de novo → restaura A, verifica,
`ROLLED_BACK`, `rollback_restored=True`. Também validados: checkpoint
corrompido/hash divergente (detectado antes de tocar o recurso),
restaurador com cobertura parcial, múltiplas versões/mais de um fix,
rollback após restart com restorer válido.

## Pendências reais
- Nenhuma integração externa real (mantido).
- `rollback()` ainda depende do chamador reconectar o
  `resource_store`/`RepairExecutor` correto após um restart (FORGE não
  pode reconstruir sozinho um recurso externo) — agora, porém, a
  AUSÊNCIA dessa reconexão é corretamente reportada como
  `ROLLBACK_FAILED` em vez de mascarada como sucesso.
- RADAR WEB COMERCIAL, Visão Evolutiva, raciocínio multimodal e
  ALL-ROUNDER não foram implementados, conforme instruído.
