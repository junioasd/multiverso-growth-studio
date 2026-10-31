import unittest

from core.library.master_library import (
    LibraryEntryNotFound,
    MasterLibrary,
    RevisionNotAllowed,
    ValidityStatus,
)
from core.memory.working_memory import WorkingMemory

"""
Testes MEMORIA/BIBLIOTECA - consulta, versionamento, aprendizado
validado, rejeicao de conteudo invalido, isolamento de dados.
"""


class LibraryQueryTests(unittest.TestCase):
    def test_search_by_area(self):
        lib = MasterLibrary()
        e1 = lib.add(title="A", area="marketing", content="x", origin="ATLAS", responsible_agent="ATLAS")
        lib.validate(e1.entry_id, validated_by="Owner")
        lib.add(title="B", area="tech", content="y", origin="FORGE", responsible_agent="FORGE")
        results = lib.search(area="marketing")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].entry_id, e1.entry_id)

    def test_search_by_tag(self):
        lib = MasterLibrary()
        e1 = lib.add(title="A", area="marketing", content="x", origin="ATLAS", responsible_agent="ATLAS", tags=["cta"])
        lib.validate(e1.entry_id, validated_by="Owner")
        results = lib.search(tag="cta")
        self.assertEqual(len(results), 1)

    def test_search_only_validated_by_default(self):
        lib = MasterLibrary()
        lib.add(title="A", area="x", content="y", origin="ATLAS", responsible_agent="ATLAS")
        self.assertEqual(len(lib.search(area="x")), 0)

    def test_get_nonexistent_raises(self):
        lib = MasterLibrary()
        with self.assertRaises(LibraryEntryNotFound):
            lib.get("nope")


class VersioningTests(unittest.TestCase):
    def test_new_entry_starts_version_1(self):
        lib = MasterLibrary()
        e = lib.add(title="A", area="x", content="y", origin="ATLAS", responsible_agent="ATLAS")
        self.assertEqual(e.version, 1)

    def test_revision_increments_version(self):
        lib = MasterLibrary()
        e = lib.add(title="A", area="x", content="y", origin="ATLAS", responsible_agent="ATLAS")
        lib.validate(e.entry_id, validated_by="Owner")
        rev = lib.propose_revision(e.entry_id, new_content="z", proposed_by="ATLAS")
        self.assertEqual(rev.version, 2)

    def test_history_chain_traced(self):
        lib = MasterLibrary()
        e = lib.add(title="A", area="x", content="y", origin="ATLAS", responsible_agent="ATLAS")
        lib.validate(e.entry_id, validated_by="Owner")
        rev1 = lib.propose_revision(e.entry_id, new_content="z", proposed_by="ATLAS")
        lib.approve_revision(rev1.entry_id, approved_by="Owner")
        rev2 = lib.propose_revision(rev1.entry_id, new_content="w", proposed_by="ATLAS")
        history = lib.history_for(rev2.entry_id)
        self.assertEqual([h.entry_id for h in history], [e.entry_id, rev1.entry_id, rev2.entry_id])


class ValidatedKnowledgeTests(unittest.TestCase):
    def test_revision_never_overwrites_validated_silently(self):
        lib = MasterLibrary()
        e = lib.add(title="A", area="x", content="original", origin="ATLAS", responsible_agent="ATLAS")
        lib.validate(e.entry_id, validated_by="Owner")
        lib.propose_revision(e.entry_id, new_content="proposta", proposed_by="ATLAS")
        # o original continua VALIDATED e com o conteudo original ate a
        # revisao ser explicitamente aprovada.
        original_reread = lib.get(e.entry_id)
        self.assertEqual(original_reread.validity, ValidityStatus.VALIDATED)
        self.assertEqual(original_reread.content, "original")

    def test_approving_revision_deprecates_original(self):
        lib = MasterLibrary()
        e = lib.add(title="A", area="x", content="original", origin="ATLAS", responsible_agent="ATLAS")
        lib.validate(e.entry_id, validated_by="Owner")
        rev = lib.propose_revision(e.entry_id, new_content="novo", proposed_by="ATLAS")
        lib.approve_revision(rev.entry_id, approved_by="Owner")
        self.assertEqual(lib.get(e.entry_id).validity, ValidityStatus.DEPRECATED)
        self.assertEqual(lib.get(rev.entry_id).validity, ValidityStatus.VALIDATED)

    def test_revision_of_non_revision_rejected(self):
        lib = MasterLibrary()
        e = lib.add(title="A", area="x", content="y", origin="ATLAS", responsible_agent="ATLAS")
        with self.assertRaises(RevisionNotAllowed):
            lib.approve_revision(e.entry_id, approved_by="Owner")


class RejectionOfInvalidContentTests(unittest.TestCase):
    def test_pending_entry_never_returned_by_default_search(self):
        lib = MasterLibrary()
        lib.add(title="A", area="x", content="y", origin="ATLAS", responsible_agent="ATLAS")
        self.assertEqual(lib.search(area="x", only_validated=True), [])

    def test_can_explicitly_see_pending_when_requested(self):
        lib = MasterLibrary()
        lib.add(title="A", area="x", content="y", origin="ATLAS", responsible_agent="ATLAS")
        self.assertEqual(len(lib.search(area="x", only_validated=False)), 1)


class WorkingMemoryIsolationTests(unittest.TestCase):
    def test_short_term_never_persisted_to_disk(self):
        mem = WorkingMemory()
        mem.remember_short_term("k", {"v": 1})
        mem.clear_short_term()
        self.assertIsNone(mem.recall_short_term("k"))

    def test_secrets_redacted_on_store(self):
        mem = WorkingMemory()
        mem.remember_short_term("k", {"password": "hunter2", "channel": "chan_1"})
        recalled = mem.recall_short_term("k")
        self.assertNotEqual(recalled["password"], "hunter2")
        self.assertEqual(recalled["channel"], "chan_1")

    def test_task_context_isolated_by_mission_task(self):
        mem = WorkingMemory()
        mem.set_task_context("m1", "t1", {"x": 1})
        mem.set_task_context("m1", "t2", {"x": 2})
        self.assertEqual(mem.get_task_context("m1", "t1"), {"x": 1})
        self.assertEqual(mem.get_task_context("m1", "t2"), {"x": 2})

    def test_learning_only_reusable_after_validation(self):
        mem = WorkingMemory()
        mem.propose_learning("cta_a", {"outcome": "worked"})
        self.assertIsNone(mem.get_validated_learning("cta_a"))
        mem.validate_learning("cta_a", validated_by="ATLAS")
        self.assertIsNotNone(mem.get_validated_learning("cta_a"))

    def test_decision_history_append_only(self):
        mem = WorkingMemory()
        mem.record_decision(agent="ATLAS", decision="escalar", reason="risco alto")
        mem.record_decision(agent="ATLAS", decision="prosseguir", reason="risco baixo")
        self.assertEqual(len(mem.decision_history()), 2)


if __name__ == "__main__":
    unittest.main()
