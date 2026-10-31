import unittest

from commercial.crm import CRM, InvalidLeadTransition
from core.models.entities import LeadStatus


class CRMTests(unittest.TestCase):
    def setUp(self):
        self.crm = CRM()

    def test_create_lead_starts_new(self):
        lead = self.crm.create_lead("chan_1", source="youtube")
        self.assertEqual(lead.status, LeadStatus.NEW)

    def test_valid_transition_sequence(self):
        lead = self.crm.create_lead("chan_1")
        self.crm.transition(lead.lead_id, LeadStatus.ENGAGED)
        self.crm.transition(lead.lead_id, LeadStatus.QUALIFIED)
        self.crm.transition(lead.lead_id, LeadStatus.OFFERED)
        self.crm.transition(lead.lead_id, LeadStatus.CUSTOMER)
        self.assertEqual(self.crm.get(lead.lead_id).status, LeadStatus.CUSTOMER)

    def test_invalid_transition_rejected(self):
        lead = self.crm.create_lead("chan_1")
        with self.assertRaises(InvalidLeadTransition):
            self.crm.transition(lead.lead_id, LeadStatus.CUSTOMER)  # não pode pular etapas

    def test_lost_is_terminal(self):
        lead = self.crm.create_lead("chan_1")
        self.crm.transition(lead.lead_id, LeadStatus.LOST)
        with self.assertRaises(InvalidLeadTransition):
            self.crm.transition(lead.lead_id, LeadStatus.ENGAGED)

    def test_by_channel_isolation(self):
        self.crm.create_lead("chan_1")
        self.crm.create_lead("chan_2")
        self.assertEqual(len(self.crm.by_channel("chan_1")), 1)
        self.assertEqual(len(self.crm.by_channel("chan_2")), 1)

    def test_funnel_counts(self):
        lead = self.crm.create_lead("chan_1")
        counts = self.crm.funnel_counts("chan_1")
        self.assertEqual(counts["NEW"], 1)


if __name__ == "__main__":
    unittest.main()
