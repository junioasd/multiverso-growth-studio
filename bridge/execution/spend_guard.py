"""
bridge.execution.spend_guard
=================================
BLOCO FINAL 2 (item 6 — OPERAÇÃO ORGÂNICA PRIMEIRO) + MICROCORREÇÕES
BF2 (single-use, strict context) + MICROCORREÇÃO (SPEND CROSS-INSTANCE
ATOMICITY — falha real confirmada por auditoria independente: duas
instâncias DIFERENTES de `SpendGuard`, compartilhando o MESMO SQLite,
confirmaram simultaneamente a mesma `escalation_id` e criaram DOIS
`approval_id` diferentes — cada um then autorizou gasto
independentemente, resultando em R$160 de gasto efetivo sobre uma
aprovação de R$100).

Causa raiz: toda a lógica de autorização (checar/criar aprovação,
checar/debitar saldo) vivia em estruturas Python EM MEMÓRIA
(`self._approvals`, `self._escalation_to_approval`) protegidas por um
`threading.Lock()` — esse lock só serializa chamadas DENTRO DA MESMA
instância; duas instâncias diferentes (mesmo processo ou processos
diferentes) têm locks completamente independentes e nenhuma visão
compartilhada do estado em memória uma da outra.

Corrigido: quando `db_path` é fornecido, TODA decisão de autorização
(criar aprovação, debitar saldo) é feita ATOMICAMENTE no PRÓPRIO
SQLite — nunca em memória:

1. `confirm_approval()`: tenta um `INSERT` simples (nunca `INSERT OR
   REPLACE`, que permitiria uma segunda linha "vencer" silenciosamente
   substituindo a primeira) contra a constraint `UNIQUE` em
   `escalation_id`. SQLite serializa escritores — entre múltiplas
   transações concorrentes tentando inserir para a MESMA
   `escalation_id` (mesmo processo com múltiplas threads/instâncias,
   OU processos diferentes), exatamente UMA consegue committar; as
   demais recebem `IntegrityError` de forma determinística e leem de
   volta a linha vencedora — todas as chamadas concorrentes devolvem
   o MESMO `approval_id`, nunca dois diferentes.
2. `record_spend()`: um único `UPDATE ... SET spent_so_far =
   spent_so_far + ? WHERE approval_id = ? AND (approved_amount -
   spent_so_far) >= ?` — checagem e débito na MESMA operação atômica
   do SQLite (nunca dois passos separados de "ler saldo" + "escrever
   saldo"). `cursor.rowcount == 0` significa que a condição de saldo
   suficiente falhou — rejeitado. SQLite serializa escritores, então
   duas tentativas concorrentes de gasto sobre o MESMO `approval_id`
   NUNCA conseguem, juntas, ultrapassar `approved_amount`.

Sem `db_path` (modo puramente em memória, um único processo/instância
— sem risco de múltiplas instâncias desconectadas), o `threading.Lock`
original continua sendo suficiente e é mantido para esse caso.
"""

from __future__ import annotations

import copy
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field

from atlas.escalation import ApprovalStatus, EscalationDesk


class SpendNotApproved(Exception):
    """Levantado quando uma tentativa de gasto não tem aprovação
    explícita e válida do Owner, ou quando o saldo restante é
    insuficiente — nunca contornável."""


class SpendContextMismatch(Exception):
    """Levantado quando `confirm_approval()` é chamado com
    `platform`/`campaign_reference`/`currency` DIFERENTE do que foi
    originalmente aprovado pelo Owner na escalação — validado em TODA
    chamada, inclusive reconfirmações."""


@dataclass
class SpendApproval:
    approval_id: str
    escalation_id: str
    platform: str
    campaign_reference: str
    approved_amount: float
    currency: str
    spent_so_far: float = 0.0
    approved_at: float = field(default_factory=time.time)

    def remaining(self) -> float:
        return max(0.0, self.approved_amount - self.spent_so_far)


def _snapshot(approval: SpendApproval) -> SpendApproval:
    """MICROCORREÇÃO (IMMUTABLE SECURITY STATE — falha crítica real
    reproduzida por auditoria independente): TODA API pública de
    `SpendGuard` que devolve um `SpendApproval` devolve uma CÓPIA
    PROFUNDA aqui, nunca a referência ao objeto interno autoritativo
    armazenado em `self._approvals`. Antes desta correção, era
    possível obter o objeto via `confirm_approval()`/`record_spend()`/
    `approval_for_escalation()` e mutar `.approved_amount`/
    `.spent_so_far` diretamente em memória, alterando o orçamento real
    usado por decisões de gasto subsequentes (bypass total do limite
    aprovado pelo Owner)."""
    return copy.deepcopy(approval)


class SpendGuard:
    """Orçamento de mídia paga SEMPRE começa em ZERO. Uma escalação
    aprovada gera EXATAMENTE uma `SpendApproval` — nunca mais de uma,
    mesmo sob múltiplas instâncias/threads/processos compartilhando o
    mesmo banco. Gasto nunca ultrapassa o valor aprovado, mesmo sob
    concorrência real entre instâncias diferentes."""

    def __init__(self, *, escalation_desk: EscalationDesk, db_path: str | None = None) -> None:
        self.escalation_desk = escalation_desk
        self.db_path = db_path
        self._approvals: dict = {}
        self._escalation_to_approval: dict = {}
        self._spend_lock = threading.Lock()
        if db_path:
            from core.models import db as db_module

            db_module.init_db(db_path)
            self._load_from_db()

    def request_spend_approval(
        self, *, platform: str, campaign_reference: str, requested_amount: float, currency: str, actor: str,
        reason: str,
    ) -> str:
        if requested_amount <= 0:
            raise ValueError("requested_amount precisa ser positivo — não faz sentido aprovar gasto zero/negativo")
        item = self.escalation_desk.escalate(
            reason=f"Aprovação de gasto solicitada: {reason}", actor=actor, operation="spend_approval", risk="HIGH",
            context={
                "platform": platform, "campaign_reference": campaign_reference, "requested_amount": requested_amount,
                "currency": currency,
            },
        )
        return item.escalation_id

    def confirm_approval(self, *, escalation_id: str, platform: str, campaign_reference: str, currency: str) -> SpendApproval:
        """MICROCORREÇÃO (cross-instance atomicity): valida o
        contexto SEMPRE contra `EscalationDesk` (fonte da verdade
        real, nunca memória local), depois cria/recupera a
        `SpendApproval` de forma ATÔMICA — quando `db_path` está
        configurado, isso acontece no PRÓPRIO SQLite (nunca em
        memória), garantindo que QUALQUER número de instâncias/
        threads/processos concorrentes para a MESMA `escalation_id`
        sempre convirjam para o MESMO `approval_id`."""
        item = self.escalation_desk.get(escalation_id)
        if item.status != ApprovalStatus.APPROVED:
            raise SpendNotApproved(
                f"escalação '{escalation_id}' não está APPROVED (status atual: {item.status.value}) — "
                f"gasto permanece bloqueado"
            )

        approved_context = item.context
        mismatches = []
        if approved_context.get("platform") != platform:
            mismatches.append(f"platform (aprovado={approved_context.get('platform')!r}, solicitado={platform!r})")
        if approved_context.get("campaign_reference") != campaign_reference:
            mismatches.append(
                f"campaign_reference (aprovado={approved_context.get('campaign_reference')!r}, "
                f"solicitado={campaign_reference!r})"
            )
        if approved_context.get("currency") != currency:
            mismatches.append(f"currency (aprovado={approved_context.get('currency')!r}, solicitado={currency!r})")
        if mismatches:
            raise SpendContextMismatch(
                f"escalação '{escalation_id}' foi aprovada para um contexto DIFERENTE — "
                f"nunca reaproveitável para outra campanha/plataforma/moeda: {'; '.join(mismatches)}"
            )

        approved_amount = float(approved_context.get("requested_amount"))

        if self.db_path:
            approval = self._confirm_approval_db(
                escalation_id=escalation_id, platform=platform, campaign_reference=campaign_reference,
                currency=currency, approved_amount=approved_amount,
            )
            # sincroniza o cache local em memória (o SQLite já é a
            # fonte da verdade atômica para o caminho com db_path --
            # esta atribuição nunca introduz uma corrida, pois todas
            # as instâncias convergem para o MESMO approval_id vindo
            # do banco).
            with self._spend_lock:
                self._approvals[approval.approval_id] = approval
                self._escalation_to_approval[escalation_id] = approval.approval_id
            return _snapshot(approval)

        # CORREÇÃO CRÍTICA (SPEND IN-MEMORY ATOMICITY — falha real
        # reproduzida por auditoria independente): o caminho em
        # memória (`db_path=None`) faz TUDO — checar existente, criar
        # se necessário, e registrar em AMBOS `_approvals` e
        # `_escalation_to_approval` — dentro de uma ÚNICA aquisição de
        # lock, sem nenhuma janela entre "criar" e "registrar". A
        # falha anterior separava esses passos em duas aquisições de
        # lock diferentes (uma dentro de `_confirm_approval_memory`,
        # outra aqui) — entre elas, múltiplas threads conseguiam
        # passar pela checagem "ainda não existe" antes de qualquer
        # uma registrar, cada uma criando seu próprio `approval_id`.
        return _snapshot(self._confirm_approval_memory(
            escalation_id=escalation_id, platform=platform, campaign_reference=campaign_reference,
            currency=currency, approved_amount=approved_amount,
        ))

    def _confirm_approval_memory(
        self, *, escalation_id: str, platform: str, campaign_reference: str, currency: str, approved_amount: float,
    ) -> SpendApproval:
        with self._spend_lock:
            existing_approval_id = self._escalation_to_approval.get(escalation_id)
            if existing_approval_id is not None:
                return self._approvals[existing_approval_id]
            approval = SpendApproval(
                approval_id=f"spend_{uuid.uuid4().hex[:12]}", escalation_id=escalation_id, platform=platform,
                campaign_reference=campaign_reference, approved_amount=approved_amount, currency=currency,
            )
            # CORREÇÃO CRÍTICA: registro em AMBOS os dicionários
            # acontece AQUI DENTRO, na MESMA aquisição de lock que fez
            # a checagem e a criação — nunca numa seção crítica
            # separada. Esta é a atomicidade que faltava.
            self._approvals[approval.approval_id] = approval
            self._escalation_to_approval[escalation_id] = approval.approval_id
            return approval

    def _confirm_approval_db(
        self, *, escalation_id: str, platform: str, campaign_reference: str, currency: str, approved_amount: float,
    ) -> SpendApproval:
        from core.models import db as db_module

        approval_id = f"spend_{uuid.uuid4().hex[:12]}"
        approved_at = time.time()
        try:
            # CORREÇÃO CRÍTICA: `INSERT` simples (NUNCA `INSERT OR
            # REPLACE`, que permitiria uma segunda transação
            # "vencedora" apagar silenciosamente a aprovação já
            # persistida por outra instância/processo). A constraint
            # `UNIQUE` em `escalation_id` faz a SEGUNDA tentativa
            # concorrente falhar com `IntegrityError` de forma
            # determinística — SQLite serializa escritores, então
            # nunca duas transações concorrentes "vencem" ao mesmo
            # tempo para a MESMA escalation_id.
            with db_module.connect(self.db_path) as conn:
                conn.execute(
                    "INSERT INTO spend_approvals "
                    "(approval_id, escalation_id, platform, campaign_reference, approved_amount, currency, "
                    "spent_so_far, approved_at) VALUES (?, ?, ?, ?, ?, ?, 0.0, ?)",
                    (approval_id, escalation_id, platform, campaign_reference, approved_amount, currency, approved_at),
                )
            return SpendApproval(
                approval_id=approval_id, escalation_id=escalation_id, platform=platform,
                campaign_reference=campaign_reference, approved_amount=approved_amount, currency=currency,
                spent_so_far=0.0, approved_at=approved_at,
            )
        except sqlite3.IntegrityError:
            # outra instância/thread/processo já criou a aprovação
            # CANÔNICA para esta escalação -- lê de volta a linha
            # vencedora (nunca cria uma segunda).
            with db_module.connect(self.db_path) as conn:
                row = conn.execute(
                    "SELECT * FROM spend_approvals WHERE escalation_id = ?", (escalation_id,),
                ).fetchone()
            if row is None:
                # janela extremamente rara -- tenta de novo
                return self._confirm_approval_db(
                    escalation_id=escalation_id, platform=platform, campaign_reference=campaign_reference,
                    currency=currency, approved_amount=approved_amount,
                )
            return self._row_to_approval(row)

    def record_spend(self, *, approval_id: str, amount: float) -> SpendApproval:
        """MICROCORREÇÃO (cross-instance atomicity): quando `db_path`
        está configurado, checagem de saldo suficiente E débito
        acontecem na MESMA operação SQL atômica — nunca dois passos
        separados de ler-depois-escrever. Duas instâncias/threads/
        processos concorrentes gastando sobre o MESMO `approval_id`
        NUNCA conseguem, juntas, ultrapassar `approved_amount`.

        MICROCORREÇÃO (SPEND IN-MEMORY ATOMICITY): o caminho em
        memória (`_record_spend_memory`) já faz a checagem de saldo +
        débito inteiramente dentro de UMA única aquisição de lock —
        nunca uma reatribuição redundante fora dela, que poderia (por
        engano, em uma futura alteração) reintroduzir uma janela como
        a corrigida em `confirm_approval()`."""
        if amount <= 0:
            raise ValueError(
                f"amount precisa ser positivo (recebido: {amount}) — gasto negativo/zero nunca é uma operação "
                f"válida e nunca pode ser usado para inflar o orçamento restante"
            )

        if self.db_path:
            approval = self._record_spend_db(approval_id=approval_id, amount=amount)
            with self._spend_lock:
                self._approvals[approval.approval_id] = approval
            return _snapshot(approval)

        return _snapshot(self._record_spend_memory(approval_id=approval_id, amount=amount))

    def _record_spend_memory(self, *, approval_id: str, amount: float) -> SpendApproval:
        with self._spend_lock:
            approval = self._approvals.get(approval_id)
            if approval is None:
                raise SpendNotApproved(
                    f"approval_id '{approval_id}' não encontrado — nenhum gasto pode ocorrer sem aprovação registrada"
                )
            if amount > approval.remaining():
                raise SpendNotApproved(
                    f"gasto de {amount} {approval.currency} excede o restante aprovado ({approval.remaining()} "
                    f"{approval.currency}) para '{approval_id}'"
                )
            approval.spent_so_far += amount
            return approval

    def _record_spend_db(self, *, approval_id: str, amount: float) -> SpendApproval:
        from core.models import db as db_module

        # CORREÇÃO CRÍTICA: checar saldo suficiente E debitar na MESMA
        # instrução UPDATE -- a condição `(approved_amount -
        # spent_so_far) >= ?` é avaliada pelo PRÓPRIO SQLite no
        # momento do commit da transação, nunca em dois passos Python
        # separados (que permitiriam duas transações concorrentes
        # lerem o MESMO saldo "antigo" antes de qualquer uma escrever).
        with db_module.connect(self.db_path) as conn:
            cursor = conn.execute(
                "UPDATE spend_approvals SET spent_so_far = spent_so_far + ? "
                "WHERE approval_id = ? AND (approved_amount - spent_so_far) >= ?",
                (amount, approval_id, amount),
            )
            rows_affected = cursor.rowcount

        if rows_affected == 0:
            # a condição falhou -- ou o approval_id nao existe, ou o
            # saldo restante e insuficiente. Le o estado atual para
            # dar um motivo preciso.
            with db_module.connect(self.db_path) as conn:
                row = conn.execute(
                    "SELECT * FROM spend_approvals WHERE approval_id = ?", (approval_id,),
                ).fetchone()
            if row is None:
                raise SpendNotApproved(
                    f"approval_id '{approval_id}' não encontrado — nenhum gasto pode ocorrer sem aprovação registrada"
                )
            approval = self._row_to_approval(row)
            raise SpendNotApproved(
                f"gasto de {amount} {approval.currency} excede o restante aprovado ({approval.remaining()} "
                f"{approval.currency}) para '{approval_id}'"
            )

        with db_module.connect(self.db_path) as conn:
            row = conn.execute("SELECT * FROM spend_approvals WHERE approval_id = ?", (approval_id,)).fetchone()
        return self._row_to_approval(row)

    def total_approved_budget(self) -> float:
        if self.db_path:
            from core.models import db as db_module

            with db_module.connect(self.db_path) as conn:
                row = conn.execute("SELECT SUM(approved_amount) AS total FROM spend_approvals").fetchone()
            return float(row["total"]) if row["total"] is not None else 0.0
        with self._spend_lock:
            return sum(a.approved_amount for a in self._approvals.values())

    def approval_for_escalation(self, escalation_id: str) -> SpendApproval | None:
        """MICROCORREÇÃO (IMMUTABLE SECURITY STATE): devolve uma CÓPIA
        PROFUNDA — mutar `approved_amount`/`spent_so_far` no objeto
        retornado nunca mais afeta o orçamento interno real."""
        if self.db_path:
            from core.models import db as db_module

            with db_module.connect(self.db_path) as conn:
                row = conn.execute(
                    "SELECT * FROM spend_approvals WHERE escalation_id = ?", (escalation_id,),
                ).fetchone()
            return _snapshot(self._row_to_approval(row)) if row is not None else None
        with self._spend_lock:
            approval_id = self._escalation_to_approval.get(escalation_id)
            found = self._approvals.get(approval_id) if approval_id else None
        return _snapshot(found) if found is not None else None

    @staticmethod
    def _row_to_approval(row) -> SpendApproval:
        return SpendApproval(
            approval_id=row["approval_id"], escalation_id=row["escalation_id"], platform=row["platform"],
            campaign_reference=row["campaign_reference"], approved_amount=row["approved_amount"],
            currency=row["currency"], spent_so_far=row["spent_so_far"], approved_at=row["approved_at"],
        )

    def _load_from_db(self) -> None:
        from core.models import db as db_module

        with db_module.connect(self.db_path) as conn:
            for row in conn.execute("SELECT * FROM spend_approvals").fetchall():
                approval = self._row_to_approval(row)
                self._approvals[approval.approval_id] = approval
                self._escalation_to_approval[approval.escalation_id] = approval.approval_id
