import time
import unittest

from commercial.finance import FinanceLedger
from commercial.sales_tracking import SalesTracker
from core.agents.runtime.contracts import AgentLimitExceeded, AgentTaskStatus
from core.agents.runtime.echo import EchoAgent
from core.agents.runtime.juris import JurisAgent
from core.agents.runtime.metrica import MetricaAgent
from core.agents.runtime.prisma import PrismaAgent
from core.agents.runtime.veritas import VeritasAgent
from core.evidence.registry import EvidenceRecord, EvidenceRegistry, VerificationStatus
from core.multimodal.evidence import Modality
from tests._evidence_test_helpers import make_test_source_registry, make_test_verifier_registry

"""
Testes BLOCO FINAL 1 (item 1) — 5 primeiros agentes REAIS
(METRICA/ECHO/VERITAS/JURIS/PRISMA): operação real sobre dados reais,
UNAVAILABLE honesto quando dependência não está conectada, rejeição
de task_type desconhecido, limites de entrada respeitados.
"""


class MetricaAgentTests(unittest.TestCase):
    def test_finance_snapshot_uses_real_ledger_data(self):
        ledger = FinanceLedger()
        ledger.record(channel_id="c1", entry_type="revenue", amount=1000.0, idempotency_key="r1")
        agent = MetricaAgent(finance_ledger=ledger)
        result = agent.execute(task_type="COMPUTE_FINANCE_SNAPSHOT", payload={"channel_id": "c1"})
        self.assertEqual(result.status, AgentTaskStatus.COMPLETED)
        self.assertEqual(result.output["revenue"], 1000.0)

    def test_unavailable_without_ledger(self):
        agent = MetricaAgent()
        result = agent.execute(task_type="COMPUTE_FINANCE_SNAPSHOT", payload={"channel_id": "c1"})
        self.assertEqual(result.status, AgentTaskStatus.UNAVAILABLE)

    def test_unknown_task_type_rejected(self):
        agent = MetricaAgent()
        result = agent.execute(task_type="NAO_EXISTE", payload={})
        self.assertEqual(result.status, AgentTaskStatus.REJECTED)

    def test_input_limit_enforced(self):
        ledger = FinanceLedger()
        agent = MetricaAgent(finance_ledger=ledger)
        result = agent.execute(task_type="COMPUTE_ROI", payload={"gain": list(range(20000)), "cost": 1})
        self.assertEqual(result.status, AgentTaskStatus.REJECTED)


class EchoAgentTests(unittest.TestCase):
    def test_full_learning_cycle(self):
        agent = EchoAgent()
        pred_ids = []
        for _ in range(6):
            r = agent.execute(task_type="RECORD_PREDICTION", payload={
                "predicting_agent_id": "ARGOS", "metric_name": "m", "predicted_value": 0.5,
                "context": "x", "scope_channel_id": "c1",
            })
            self.assertEqual(r.status, AgentTaskStatus.COMPLETED)
            pred_ids.append(r.output["prediction_id"])
        for pid in pred_ids:
            r = agent.execute(task_type="RECORD_OUTCOME", payload={"prediction_id": pid, "actual_value": 0.6})
            self.assertEqual(r.status, AgentTaskStatus.COMPLETED)
        r = agent.execute(task_type="EVALUATE_LEARNING", payload={"metric_name": "m", "scope_channel_id": "c1"})
        self.assertEqual(r.output["status"], "VALIDATED")
        r = agent.execute(task_type="PROMOTE_LEARNING", payload={
            "metric_name": "m", "statement": "x", "validated_by": "ECHO", "scope_channel_id": "c1",
        })
        self.assertEqual(r.status, AgentTaskStatus.COMPLETED)

    def test_scope_isolation_via_agent(self):
        agent = EchoAgent()
        for _ in range(6):
            r = agent.execute(task_type="RECORD_PREDICTION", payload={
                "predicting_agent_id": "ARGOS", "metric_name": "m", "predicted_value": 0.5,
                "context": "x", "scope_channel_id": "c1",
            })
            agent.execute(task_type="RECORD_OUTCOME", payload={"prediction_id": r.output["prediction_id"], "actual_value": 0.6})
        agent.execute(task_type="PROMOTE_LEARNING", payload={
            "metric_name": "m", "statement": "x", "validated_by": "ECHO", "scope_channel_id": "c1",
        })
        r = agent.execute(task_type="GET_VISIBLE_LEARNINGS", payload={"scope_channel_id": "c2"})
        self.assertEqual(r.output["learnings"], [])

    def test_outcome_without_prediction_rejected_not_crashed(self):
        agent = EchoAgent()
        r = agent.execute(task_type="RECORD_OUTCOME", payload={"prediction_id": "inexistente", "actual_value": 1.0})
        self.assertEqual(r.status, AgentTaskStatus.REJECTED)


class VeritasAgentTests(unittest.TestCase):
    def _registry_with_evidence(self):
        registry = EvidenceRegistry(verifier_registry=make_test_verifier_registry(), source_registry=make_test_source_registry())
        registry.register(EvidenceRecord(
            evidence_id="ev1", type="x", source="fonte_real", provenance="x", observed_at=time.time(),
            modality=Modality.DOCUMENT, confidence=0.9, verifier="teste_automatizado",
            verification_method="validacao_manual", requested_verification_status=VerificationStatus.VERIFIED,
        ))
        return registry

    def test_claim_with_real_evidence_supported(self):
        agent = VeritasAgent(evidence_registry=self._registry_with_evidence())
        r = agent.execute(task_type="VERIFY_CLAIM", payload={"evidence_ids": ["ev1"]})
        self.assertEqual(r.output["verdict"], "SUPPORTED")

    def test_claim_without_evidence_unverifiable_not_fabricated(self):
        agent = VeritasAgent(evidence_registry=self._registry_with_evidence())
        r = agent.execute(task_type="VERIFY_CLAIM", payload={"evidence_ids": ["nao_existe"]})
        self.assertEqual(r.output["verdict"], "UNVERIFIABLE")
        self.assertEqual(r.confidence, 0.0)

    def test_unavailable_without_registry(self):
        agent = VeritasAgent()
        r = agent.execute(task_type="VERIFY_CLAIM", payload={"evidence_ids": ["ev1"]})
        self.assertEqual(r.status, AgentTaskStatus.UNAVAILABLE)

    def test_empty_evidence_ids_rejected(self):
        agent = VeritasAgent(evidence_registry=self._registry_with_evidence())
        r = agent.execute(task_type="VERIFY_CLAIM", payload={"evidence_ids": []})
        self.assertEqual(r.status, AgentTaskStatus.REJECTED)


class JurisAgentTests(unittest.TestCase):
    def test_blocks_unlicensed_ip_use(self):
        agent = JurisAgent()
        r = agent.execute(task_type="CHECK_COMPLIANCE", payload={
            "item": {"uses_third_party_brand_or_ip": True, "has_license": False},
        })
        self.assertEqual(r.output["verdict"], "BLOCKED")

    def test_no_known_violation_never_means_fully_approved(self):
        """`NO_KNOWN_VIOLATION` é o veredito quando nenhuma regra
        dispara — nunca `APPROVED`, para nunca sugerir aprovação
        jurídica completa."""
        agent = JurisAgent()
        r = agent.execute(task_type="CHECK_COMPLIANCE", payload={"item": {}})
        self.assertEqual(r.output["verdict"], "NO_KNOWN_VIOLATION")
        self.assertNotEqual(r.output["verdict"], "APPROVED")

    def test_list_rules_returns_real_auditable_rules(self):
        agent = JurisAgent()
        r = agent.execute(task_type="LIST_RULES", payload={})
        self.assertGreaterEqual(len(r.output["rules"]), 4)


class PrismaAgentTests(unittest.TestCase):
    def test_describe_series_real_stats(self):
        agent = PrismaAgent()
        r = agent.execute(task_type="DESCRIBE_SERIES", payload={"values": [10, 20, 30]})
        self.assertEqual(r.output["mean"], 20)

    def test_insufficient_sample_never_fabricates_stats(self):
        agent = PrismaAgent()
        r = agent.execute(task_type="DESCRIBE_SERIES", payload={"values": [1]})
        self.assertEqual(r.output["status"], "INSUFFICIENT_DATA")

    def test_outlier_detection_real(self):
        agent = PrismaAgent()
        r = agent.execute(task_type="DETECT_OUTLIERS", payload={"values": [5, 5, 5, 5, 5, 5, 5, 5, 5, 1000]})
        self.assertGreaterEqual(len(r.output["outliers"]), 1)


if __name__ == "__main__":
    unittest.main()
