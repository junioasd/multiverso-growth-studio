import unittest

from commercial.crm import CRM
from commercial.finance import FinanceLedger
from commercial.sales_tracking import SalesTracker
from core.agents.runtime.contracts import AgentTaskStatus
from core.agents.runtime.libra import LibraAgent
from core.agents.runtime.mercury import MercuryAgent
from core.agents.runtime.nexus import NexusAgent
from core.agents.runtime.relay import RelayAgent
from core.library.master_library import MasterLibrary
from core.models.entities import LeadStatus

"""
Testes BLOCO FINAL 1 (item 1) — LIBRA/MERCURY/NEXUS/RELAY: operação
real sobre sistemas já existentes (MasterLibrary/CRM/FinanceLedger/
SalesTracker), UNAVAILABLE honesto, nunca fabrica taxas de conversão
ou comparações entre canais sem dados.
"""


class LibraAgentTests(unittest.TestCase):
    def test_add_then_validate_cycle(self):
        agent = LibraAgent(library=MasterLibrary())
        added = agent.execute(task_type="ADD_ENTRY", payload={
            "title": "t", "area": "a", "content": "c", "origin": "o", "responsible_agent": "LIBRA",
        })
        self.assertEqual(added.status, AgentTaskStatus.COMPLETED)
        self.assertEqual(added.output["validity"], "PENDING_REVIEW")
        validated = agent.execute(task_type="VALIDATE_ENTRY", payload={
            "entry_id": added.output["entry_id"], "validated_by": "owner",
        })
        self.assertEqual(validated.output["validity"], "VALIDATED")

    def test_unavailable_without_library(self):
        agent = LibraAgent()
        r = agent.execute(task_type="ADD_ENTRY", payload={"title": "t", "area": "a", "content": "c", "origin": "o", "responsible_agent": "x"})
        self.assertEqual(r.status, AgentTaskStatus.UNAVAILABLE)

    def test_validate_unknown_entry_rejected(self):
        agent = LibraAgent(library=MasterLibrary())
        r = agent.execute(task_type="VALIDATE_ENTRY", payload={"entry_id": "nao_existe", "validated_by": "x"})
        self.assertEqual(r.status, AgentTaskStatus.REJECTED)


class MercuryAgentTests(unittest.TestCase):
    def test_compare_channels_excludes_channels_without_data(self):
        ledger = FinanceLedger()
        ledger.record(channel_id="c1", entry_type="revenue", amount=500.0, idempotency_key="r1")
        agent = MercuryAgent(finance_ledger=ledger)
        r = agent.execute(task_type="COMPARE_CHANNELS", payload={"channel_ids": ["c1", "c2_vazio"]})
        self.assertEqual(r.status, AgentTaskStatus.COMPLETED)
        self.assertEqual(len(r.output["ranked_by_revenue"]), 1)
        self.assertIn("c2_vazio", r.output["channels_without_data"])

    def test_consolidated_overview_reports_missing_systems(self):
        agent = MercuryAgent(finance_ledger=FinanceLedger())
        r = agent.execute(task_type="CONSOLIDATED_OVERVIEW", payload={"channel_id": "c1"})
        self.assertEqual(r.status, AgentTaskStatus.COMPLETED)
        self.assertIn("crm", r.output["unavailable_systems"])
        self.assertIn("sales_tracker", r.output["unavailable_systems"])

    def test_fully_unavailable_with_nothing_connected(self):
        agent = MercuryAgent()
        r = agent.execute(task_type="CONSOLIDATED_OVERVIEW", payload={"channel_id": "c1"})
        self.assertEqual(r.status, AgentTaskStatus.UNAVAILABLE)


class NexusAgentTests(unittest.TestCase):
    def _crm_with_funnel(self):
        crm = CRM()
        for i in range(10):
            lead = crm.create_lead("c1", idempotency_key=f"l{i}", source="x")
            if i < 4:
                crm.transition(lead.lead_id, LeadStatus.ENGAGED)
        return crm

    def test_conversion_rate_computed_from_real_counts(self):
        agent = NexusAgent(crm=self._crm_with_funnel())
        r = agent.execute(task_type="ANALYZE_FUNNEL_CONVERSION", payload={"channel_id": "c1"})
        self.assertEqual(r.output["conversion_rates"]["NEW->ENGAGED"], 0.4)

    def test_undefined_when_no_leads_at_stage(self):
        agent = NexusAgent(crm=CRM())
        r = agent.execute(task_type="ANALYZE_FUNNEL_CONVERSION", payload={"channel_id": "empty_channel"})
        self.assertEqual(r.output["conversion_rates"]["NEW->ENGAGED"], "UNDEFINED")

    def test_unavailable_without_crm(self):
        agent = NexusAgent()
        r = agent.execute(task_type="ANALYZE_FUNNEL_CONVERSION", payload={"channel_id": "c1"})
        self.assertEqual(r.status, AgentTaskStatus.UNAVAILABLE)


class RelayAgentTests(unittest.TestCase):
    def test_valid_transition_succeeds(self):
        crm = CRM()
        lead = crm.create_lead("c1", idempotency_key="l1", source="x")
        agent = RelayAgent(crm=crm)
        r = agent.execute(task_type="TRANSITION_LEAD", payload={"lead_id": lead.lead_id, "new_status": "ENGAGED"})
        self.assertEqual(r.status, AgentTaskStatus.COMPLETED)

    def test_invalid_transition_rejected_not_forced(self):
        crm = CRM()
        lead = crm.create_lead("c1", idempotency_key="l1", source="x")
        agent = RelayAgent(crm=crm)
        r = agent.execute(task_type="TRANSITION_LEAD", payload={"lead_id": lead.lead_id, "new_status": "CUSTOMER"})
        self.assertEqual(r.status, AgentTaskStatus.REJECTED)

    def test_stalled_leads_never_includes_terminal_states(self):
        crm = CRM()
        lead1 = crm.create_lead("c1", idempotency_key="l1", source="x")
        lead2 = crm.create_lead("c1", idempotency_key="l2", source="x")
        crm.transition(lead2.lead_id, LeadStatus.LOST)
        agent = RelayAgent(crm=crm)
        r = agent.execute(task_type="IDENTIFY_STALLED_LEADS", payload={"channel_id": "c1"})
        stalled_ids = [l["lead_id"] for l in r.output["stalled_leads"]]
        self.assertIn(lead1.lead_id, stalled_ids)
        self.assertNotIn(lead2.lead_id, stalled_ids)


if __name__ == "__main__":
    unittest.main()
