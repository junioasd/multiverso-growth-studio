# CHANGELOG — Microcorreção BF2: Spend Cross-Instance Atomicity

Base: `MULTIVERSO_GROWTH_STUDIO_MASTER_V1_EXPANSION_V6_1_1_BF2_ATOMIC_ANTIREPLAY`
(1485 testes). Corrige a falha real reproduzida pela auditoria,
preservando tudo já aprovado.

## Resultado real — 3 execuções consecutivas, verificação máxima

```
COLLECTED: 1493  PASS: 1493  FAIL: 0  ERROR: 0  SKIP: 0
DURATION: ~75-77s (3 execuções consecutivas, -W error::ResourceWarning)
EXIT CODE: 0 (as 3 vezes)
Arquivos órfãos: 0
```

**1493 testes** (1485 preservados + **8 testes novos**).

## Falha reproduzida antes de corrigir

Reproduzi o EXATO cenário reportado: duas instâncias DIFERENTES de
`SpendGuard`, compartilhando o MESMO `db_path`, confirmando
simultaneamente a mesma `escalation_id` — resultado antes da correção:
dois `approval_id` diferentes, cada um autorizando gasto
independentemente (R$80 + R$80 = R$160 sobre uma aprovação de R$100).

## Causa raiz

Toda a lógica de autorização (`confirm_approval`, `record_spend`)
vivia em estruturas Python EM MEMÓRIA (`self._approvals`,
`self._escalation_to_approval`), protegidas por um
`threading.Lock()` — esse lock só serializa chamadas DENTRO DA MESMA
instância. Duas instâncias diferentes (mesmo processo ou processos
diferentes) têm locks completamente independentes e nenhuma visão
compartilhada uma da outra — exatamente a mesma classe de falha já
corrigida no `NavigatorReal` na rodada anterior, mas que eu não havia
generalizado para o `SpendGuard` na época.

## Correção aplicada

Quando `db_path` é fornecido, TODA decisão de autorização agora é
feita ATOMICAMENTE no PRÓPRIO SQLite — nunca em memória:

1. **`confirm_approval()`**: `INSERT` simples (NUNCA `INSERT OR
   REPLACE`, que permitiria uma segunda transação "vencedora" apagar
   silenciosamente a aprovação já persistida por outra instância)
   contra a constraint `UNIQUE` já existente em `escalation_id`.
   SQLite serializa escritores — entre múltiplas transações
   concorrentes tentando inserir para a MESMA `escalation_id`
   (mesmo processo com múltiplas instâncias, OU processos
   diferentes), exatamente UMA consegue committar; as demais recebem
   `IntegrityError` de forma determinística e leem de volta a linha
   vencedora — todas as chamadas concorrentes devolvem o MESMO
   `approval_id`.
2. **`record_spend()`**: um único
   `UPDATE spend_approvals SET spent_so_far = spent_so_far + ?
   WHERE approval_id = ? AND (approved_amount - spent_so_far) >= ?`
   — checagem de saldo suficiente E débito na MESMA operação atômica
   do SQLite, nunca dois passos separados de ler-depois-escrever.
   `cursor.rowcount == 0` sinaliza saldo insuficiente. SQLite
   serializa escritores, então duas tentativas concorrentes de gasto
   sobre o MESMO `approval_id` nunca conseguem, juntas, ultrapassar
   `approved_amount`.

Sem `db_path` (modo puramente em memória, um único
processo/instância), o `threading.Lock` original continua suficiente
e foi preservado para esse caso.

## Validado com concorrência real (múltiplas instâncias genuínas)

- **2, 10 e 50 instâncias** de `SpendGuard` (nunca a mesma instância
  reaproveitada), confirmando concorrentemente a mesma escalação →
  sempre exatamente 1 `approval_id`, testado e confirmado nas 3
  escalas.
- **Cenário exato do auditor**: gasto concorrente de R$80 + R$80
  sobre uma aprovação de R$100 → exatamente 1 sucesso, 1 rejeitado,
  total final sempre R$80 (nunca R$160).
- **Teste de estresse adicional**: 30 tentativas de R$10 vindas de 30
  instâncias diferentes sobre uma aprovação de R$100 → exatamente 10
  tentativas aceitas (R$100 exato), 20 rejeitadas — testado e
  confirmado.
- **Restart**: nova instância após reiniciar devolve o MESMO
  `approval_id` com o saldo já gasto preservado.

## Arquivos alterados
- `bridge/execution/spend_guard.py` — reescrito: toda decisão de
  autorização passa pelo SQLite atomicamente quando `db_path` está
  configurado (`INSERT` simples + `UPDATE` condicional), modo em
  memória preservado para uso single-instância

## Arquivos novos
- `tests/test_microcorrecao_spend_cross_instance_atomicity.py` (8 testes)

**Nenhuma funcionalidade já aprovada foi removida.** Todos os testes
anteriores de `SpendGuard` (single-instância, com e sem `db_path`)
continuam passando sem alteração — a API pública
(`confirm_approval`/`record_spend`/`total_approved_budget`/
`approval_for_escalation`) permanece idêntica; só a implementação
interna mudou para ser genuinamente atômica entre instâncias.

## Pendências reais (honestamente declaradas)
- `pytest` continua indisponível neste ambiente
