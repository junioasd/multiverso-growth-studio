# CHANGELOG — Microcorreção BF2: Immutable Security State

Base: `MULTIVERSO_GROWTH_STUDIO_MASTER_V1_EXPANSION_V6_1_1_BF2_SPEND_IN_MEMORY`
(1499 testes). Corrige a falha crítica real reproduzida pela
auditoria, preservando tudo já aprovado — incluindo todos os testes
de concorrência das rodadas anteriores.

## Resultado real — 3 execuções consecutivas, verificação máxima

```
COLLECTED: 1515  PASS: 1515  FAIL: 0  ERROR: 0  SKIP: 0
DURATION: ~76-78s (3 execuções consecutivas, -W error::ResourceWarning)
EXIT CODE: 0 (as 3 vezes)
Arquivos órfãos: 0
```

**1515 testes** (1499 preservados + **16 testes novos**).

## Falha crítica confirmada

`EscalationDesk` e `SpendGuard` devolviam a REFERÊNCIA DIRETA ao
objeto interno autoritativo (`EscalationItem`/`SpendApproval`) em
TODA API pública — não uma cópia. Isso permitia:

1. Obter um `EscalationItem` via `get()` e mutar `.status` de
   `PENDING_OWNER_APPROVAL` para `APPROVED` diretamente em memória,
   OU mutar `.context["requested_amount"]` de R$100 para R$10.000 —
   sem NENHUMA decisão real do Owner — e o estado interno usado por
   `SpendGuard.confirm_approval()` (que chama `escalation_desk.get()`
   internamente) via a MESMA referência, aceitava a adulteração.
2. Obter um `SpendApproval` via `confirm_approval()`/`record_spend()`/
   `approval_for_escalation()` e mutar `.approved_amount` de R$100
   para R$1.000 diretamente — o orçamento real usado por
   `record_spend()` (mesma referência) aceitava o valor adulterado.

## Correção aplicada

TODA API pública de `EscalationDesk` (`escalate`, `approve`, `reject`,
`cancel`, `resolve`, `expire_stale`, `get`, `pending`, `all`,
`find_by_idempotency_key`) e `SpendGuard` (`confirm_approval`,
`record_spend`, `approval_for_escalation`) agora devolve uma **CÓPIA
PROFUNDA** (`copy.deepcopy()`) do objeto interno, nunca a referência
real — via novos helpers `_snapshot()`/`_snapshot_list()` em cada
módulo, aplicados no ponto de retorno de cada método público.
`copy.deepcopy()` copia recursivamente `context`/`evidence` (dicts e
listas aninhados) — mutar QUALQUER campo, em qualquer profundidade,
no objeto retornado NUNCA mais afeta o estado interno.

A lógica INTERNA de cada classe continua operando sobre os objetos
REAIS armazenados em `self._items`/`self._approvals` (necessário para
mutação legítima, ex: `SpendApproval.spent_so_far += amount` dentro
de `_record_spend_memory`) — apenas o VALOR DE RETORNO nos limites da
API pública passou a ser sempre uma cópia independente.

## Validado com os cenários exatos reportados

- Mutar `status` retornado para `APPROVED` sem decisão real do Owner
  → estado interno continua `PENDING_OWNER_APPROVAL`.
- Mutar `context["requested_amount"]` de 100 para 10000 → estado
  interno continua 100.
- Mutar `approved_amount` de 100 para 1000 → orçamento interno
  continua 100; tentativa de gastar 500 sobre o valor adulterado
  continua bloqueada.
- Mutar `spent_so_far` para valor negativo (tentando inflar o
  restante) → saldo interno nunca é afetado.
- Mutação de `context`/`evidence` aninhados (dict dentro de dict,
  listas) → nunca afeta o estado interno.
- Validado em AMBOS os modos: em memória (`db_path=None`) e SQLite.
- Duas confirmações da mesma aprovação devolvem objetos INDEPENDENTES
  (mesmo `approval_id`, identidades de objeto diferentes) — mutar um
  nunca afeta o outro nem o interno.

## Preservação confirmada explicitamente

Reexecutei os 4 arquivos de teste de concorrência das rodadas
anteriores (`test_microcorrecao_spend_in_memory_atomicity`,
`test_microcorrecao_spend_cross_instance_atomicity`,
`test_microcorrecao_atomic_antireplay_strict_spend`,
`test_microcorrecao_bf2_owner_gate_antireplay`) — **48 testes, todos
passando sem alteração**. A correção de imutabilidade é ortogonal à
atomicidade já corrigida: os objetos retornados são cópias, mas o
CONTEÚDO de cada cópia continua refletindo corretamente o resultado
atômico já garantido internamente.

## Arquivos alterados
- `atlas/escalation.py` — `_snapshot()`/`_snapshot_list()` (novos
  helpers), aplicados em `escalate`/`approve`/`reject`/`cancel`/
  `expire_stale`/`get`/`pending`/`all`/`find_by_idempotency_key`
- `bridge/execution/spend_guard.py` — `_snapshot()` (novo helper),
  aplicado em `confirm_approval`/`record_spend`/
  `approval_for_escalation`

## Arquivos novos
- `tests/test_microcorrecao_immutable_security_state.py` (16 testes)

**Nenhuma funcionalidade já aprovada foi removida.** A API pública
(assinaturas de método, tipos de retorno) permanece idêntica — apenas
o objeto retornado passou a ser uma cópia independente em vez da
referência interna. Todo código que apenas LÊ campos do objeto
retornado (o padrão de uso em toda a base) continua funcionando
identicamente.

## Pendências reais (honestamente declaradas)
- `pytest` continua indisponível neste ambiente
