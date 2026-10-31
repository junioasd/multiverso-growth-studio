import os
import tempfile
import unittest

from atlas.escalation import ApprovalStatus, EscalationDesk
from bridge.execution.spend_guard import SpendGuard, SpendNotApproved
from core.models import db as db_module
from core.security.audit import AuditTrail
from core.security.auth import Principal, Role

"""
MICROCORREÇÃO — DEFENSIVE COPY ON INPUT.

Falha real reproduzida por auditoria independente: `EscalationDesk`
protegia os objetos RETORNADOS (correção anterior), mas
`context`/`evidence` recebidos na ENTRADA ainda podiam compartilhar
referências mutáveis com o estado interno — `sanitize_dict()` recria
dicts recursivamente, mas devolve `list` (e qualquer coisa dentro
dela, ex: `list[dict]`) por referência direta. Mutar a lista/dict
ORIGINAL que o chamador ainda segura, depois de `escalate()` retornar,
mutava o estado interno através dessa referência compartilhada.

Corrigido: `copy.deepcopy()` aplicado ao resultado já sanitizado,
antes de armazenar em `EscalationItem` — nenhuma referência mutável
externa permanece ligada ao estado interno, desde a criação.
"""

OWNER = Principal(subject="owner@x.com", role=Role.OWNER, scopes={"*"})


class DefensiveCopyOnInputMemoryTests(unittest.TestCase):
    def setUp(self):
        self.audit = AuditTrail()
        self.desk = EscalationDesk(self.audit)

    def test_list_of_dicts_in_context_mutation_after_escalate_never_affects_internal(self):
        """Cenário EXATO reportado: list[dict] em context, mutado
        depois de escalate() retornar."""
        original_items = [{"produto": "A", "valor": 100}]
        item = self.desk.escalate(reason="x", actor="x", operation="x", context={"items": original_items})

        original_items[0]["valor"] = 999999
        original_items.append({"produto": "B", "valor": 50000})

        fresh = self.desk.get(item.escalation_id)
        self.assertEqual(fresh.context["items"], [{"produto": "A", "valor": 100}])

    def test_nested_lists_in_context_mutation_never_affects_internal(self):
        original_nested = [[1, 2], [3, 4]]
        item = self.desk.escalate(reason="x", actor="x", operation="x", context={"matrix": original_nested})

        original_nested[0].append(999)
        original_nested.append([5, 6])

        fresh = self.desk.get(item.escalation_id)
        self.assertEqual(fresh.context["matrix"], [[1, 2], [3, 4]])

    def test_evidence_with_list_of_dicts_mutation_never_affects_internal(self):
        """Cenário EXATO reportado: evidence com list[dict]."""
        original_proofs = [{"score": 0.9}, {"score": 0.8}]
        item = self.desk.escalate(reason="x", actor="x", operation="x", context={}, evidence={"proofs": original_proofs})

        original_proofs[0]["score"] = 0.0
        original_proofs.append({"score": 1.0})

        fresh = self.desk.get(item.escalation_id)
        self.assertEqual(fresh.evidence["proofs"], [{"score": 0.9}, {"score": 0.8}])

    def test_mutating_original_dict_argument_itself_never_affects_internal(self):
        """Mutar o dict `context` ORIGINAL (não apenas valores
        aninhados) após escalate() — adicionar/remover chaves."""
        original_context = {"key1": "value1"}
        item = self.desk.escalate(reason="x", actor="x", operation="x", context=original_context)

        original_context["key1"] = "adulterado"
        original_context["key2"] = "novo campo injetado"

        fresh = self.desk.get(item.escalation_id)
        self.assertEqual(fresh.context, {"key1": "value1"})

    def test_set_in_context_never_shares_reference(self):
        original_set = {1, 2, 3}
        item = self.desk.escalate(reason="x", actor="x", operation="x", context={"tags": original_set})

        original_set.add(999)

        fresh = self.desk.get(item.escalation_id)
        self.assertEqual(fresh.context["tags"], {1, 2, 3})

    def test_tuple_containing_mutable_dict_never_shares_reference(self):
        inner_dict = {"x": 1}
        original_tuple = (inner_dict, "static")
        item = self.desk.escalate(reason="x", actor="x", operation="x", context={"pair": original_tuple})

        inner_dict["x"] = 999  # muta o dict referenciado PELA tupla original

        fresh = self.desk.get(item.escalation_id)
        self.assertEqual(fresh.context["pair"][0], {"x": 1})

    def test_full_attack_scenario_end_to_end_with_spend_guard(self):
        """Cenário completo de ponta a ponta: cria aprovação de gasto
        com context contendo estrutura aninhada, muta o ORIGINAL
        depois — SpendGuard nunca vê o valor adulterado."""
        original_context = {
            "platform": "google_ads", "campaign_reference": "camp1", "requested_amount": 100.0, "currency": "BRL",
            "audit_trail": [{"note": "solicitação original"}],
        }
        item = self.desk.escalate(reason="x", actor="x", operation="spend_approval", context=original_context)
        self.desk.approve(item.escalation_id, decision="ok", resolved_by=OWNER)

        # muta o ORIGINAL depois de aprovado
        original_context["requested_amount"] = 999999.0
        original_context["audit_trail"][0]["note"] = "adulterado"
        original_context["audit_trail"].append({"note": "injetado"})

        guard = SpendGuard(escalation_desk=self.desk, db_path=None)
        approval = guard.confirm_approval(
            escalation_id=item.escalation_id, platform="google_ads", campaign_reference="camp1", currency="BRL",
        )
        self.assertEqual(approval.approved_amount, 100.0)

        with self.assertRaises(SpendNotApproved):
            guard.record_spend(approval_id=approval.approval_id, amount=5000.0)


class DefensiveCopyOnInputSQLiteTests(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db_module.remove_db_file(self.db_path)
        db_module.init_db(self.db_path)
        self.audit = AuditTrail()
        self.desk = EscalationDesk(self.audit, db_path=self.db_path)

    def tearDown(self):
        db_module.remove_db_file(self.db_path)

    def test_list_of_dicts_in_context_mutation_never_affects_internal_sqlite(self):
        original_items = [{"produto": "A", "valor": 100}]
        item = self.desk.escalate(reason="x", actor="x", operation="x", context={"items": original_items})

        original_items[0]["valor"] = 999999
        original_items.append({"produto": "B"})

        fresh = self.desk.get(item.escalation_id)
        self.assertEqual(fresh.context["items"], [{"produto": "A", "valor": 100}])

    def test_evidence_with_nested_structures_never_affects_internal_sqlite(self):
        original_evidence = {"proofs": [{"score": 0.9, "tags": ["a", "b"]}]}
        item = self.desk.escalate(reason="x", actor="x", operation="x", context={}, evidence=original_evidence)

        original_evidence["proofs"][0]["score"] = 0.0
        original_evidence["proofs"][0]["tags"].append("injected")

        fresh = self.desk.get(item.escalation_id)
        self.assertEqual(fresh.evidence["proofs"], [{"score": 0.9, "tags": ["a", "b"]}])

    def test_full_attack_scenario_end_to_end_sqlite(self):
        original_context = {
            "platform": "google_ads", "campaign_reference": "camp1", "requested_amount": 100.0, "currency": "BRL",
            "metadata": [{"note": "original"}],
        }
        item = self.desk.escalate(reason="x", actor="x", operation="spend_approval", context=original_context)
        self.desk.approve(item.escalation_id, decision="ok", resolved_by=OWNER)

        original_context["requested_amount"] = 999999.0
        original_context["metadata"][0]["note"] = "adulterado"

        guard = SpendGuard(escalation_desk=self.desk, db_path=self.db_path)
        approval = guard.confirm_approval(
            escalation_id=item.escalation_id, platform="google_ads", campaign_reference="camp1", currency="BRL",
        )
        self.assertEqual(approval.approved_amount, 100.0)


if __name__ == "__main__":
    unittest.main()
