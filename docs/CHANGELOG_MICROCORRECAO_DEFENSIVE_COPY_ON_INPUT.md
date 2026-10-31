# CHANGELOG — Microcorreção BF2: Defensive Copy on Input

Base: `MULTIVERSO_GROWTH_STUDIO_MASTER_V1_EXPANSION_V6_1_1_BF2_IMMUTABLE_STATE`
(1515 testes). Corrige a falha real reproduzida pela auditoria,
preservando tudo já aprovado no BLOCO FINAL 2 — incluindo os 64
testes de imutabilidade/Owner Gate/anti-replay/SpendGuard/concorrência
das rodadas anteriores.

## Resultado real — 3 execuções consecutivas, verificação máxima

```
COLLECTED: 1525  PASS: 1525  FAIL: 0  ERROR: 0  SKIP: 0
DURATION: ~76-82s (3 execuções consecutivas, -W error::ResourceWarning)
EXIT CODE: 0 (as 3 vezes)
Arquivos órfãos: 0
```

**1525 testes** (1515 preservados + **10 testes novos**).

## Falha real confirmada

A correção anterior (`IMMUTABLE SECURITY STATE`) protegeu os OBJETOS
RETORNADOS por `EscalationDesk`/`SpendGuard` — mutar o que essas APIs
devolvem nunca mais afeta o estado interno. Mas `context`/`evidence`
recebidos na ENTRADA de `escalate()` ainda podiam compartilhar
referências mutáveis com o estado interno: `sanitize_dict()` (usado
para redigir segredos antes de armazenar) recria `dict`s
recursivamente, mas devolve valores `list` (e qualquer coisa dentro
deles — `list[dict]`, listas aninhadas, etc.) **por referência
direta**. Reproduzido: criar uma escalação com `context={"items":
[{"valor": 100}]}`, depois mutar a lista/dict ORIGINAL que o chamador
ainda segurava — a mutação refletia no estado interno via
`desk.get()`, sem NENHUMA operação autorizada.

## Correção aplicada

`copy.deepcopy()` aplicado ao resultado JÁ sanitizado
(`sanitize_dict(context)`/`sanitize_dict(evidence or {})`), no
momento da construção do `EscalationItem` dentro de `escalate()` —
o ÚNICO ponto de construção usado por TODOS os chamadores (inclusive
`SpendGuard.request_spend_approval()` e `NavigatorReal`'s escalações
de ação crítica, que passam `params`/contexto arbitrário do
chamador). `copy.deepcopy()` copia recursivamente `dict`, `list`,
`tuple`, `set` e qualquer estrutura aninhada permitida — nenhuma
referência mutável externa permanece ligada ao estado interno desde
a criação, em nenhuma profundidade.

A redação de segredos de `sanitize_dict()` é preservada integralmente
(roda primeiro, `deepcopy()` só congela o resultado já redigido).

## Validado com os cenários exatos reportados

- `list[dict]` em `context`, mutado depois de `escalate()` retornar
  → estado interno nunca afetado.
- Listas aninhadas (`list[list[int]]`) → nunca afetadas.
- `evidence` com `list[dict]` → nunca afetado.
- Mutar o próprio dict `context` original (adicionar/remover chaves)
  → nunca afetado.
- `set` e `tuple` contendo dict mutável → nunca afetados.
- Cenário de ataque completo, de ponta a ponta, com `SpendGuard`:
  criar aprovação de gasto com contexto aninhado, mutar o original
  depois de aprovado, confirmar — orçamento interno permanece o
  original, tentativa de gasto acima do valor real continua bloqueada.
- Validado em AMBOS os modos: memória e SQLite.

## Preservação confirmada explicitamente

Reexecutei os 5 arquivos de teste de segurança/concorrência das
rodadas anteriores do BLOCO FINAL 2
(`test_microcorrecao_immutable_security_state`,
`test_microcorrecao_bf2_owner_gate_antireplay`,
`test_microcorrecao_atomic_antireplay_strict_spend`,
`test_microcorrecao_spend_cross_instance_atomicity`,
`test_microcorrecao_spend_in_memory_atomicity`) — **64 testes, todos
passando sem alteração**.

## Outros estados críticos revisados

Revisei `TokenVault.store_token()` (`scopes: list`) — já reconstrói
uma nova lista (`list(scopes or [])`) na entrada; dado que scopes são
sempre strings (imutáveis) na prática, não há vetor de ataque
equivalente ali. `SpendApproval` não tem nenhum campo `dict`/`list` —
inerentemente livre deste tipo de risco. O único ponto real de
construção com campos `dict` mutáveis a partir de entrada de chamador
é `EscalationItem` via `escalate()`, agora corrigido.

## Arquivos alterados
- `atlas/escalation.py` — `escalate()`: `copy.deepcopy()` aplicado a
  `context`/`evidence` já sanitizados, antes de armazenar

## Arquivos novos
- `tests/test_microcorrecao_defensive_copy_on_input.py` (10 testes)

**Nenhuma funcionalidade já aprovada foi removida.** Assinatura de
`escalate()` e comportamento de redação de segredos permanecem
idênticos — apenas a independência de referência da entrada foi
reforçada.

## Pendências reais (honestamente declaradas)
- `pytest` continua indisponível neste ambiente
