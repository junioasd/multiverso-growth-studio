import unittest

from api.bootstrap import build_app
from core.agents.runtime.contracts import AgentTaskStatus
from core.security.auth import hash_password

"""
Teste BLOCO FINAL 1 (item 1) — verifica que os 20 agentes reais estão
genuinamente conectados a `AtlasDirector`, usando os sistemas internos
JÁ REAIS (CRM/Finance/Sales/MasterLibrary) desta mesma inicialização
— nunca um sistema paralelo fabricado.
"""


class AtlasDirectorRealAgentsWiringTests(unittest.TestCase):
    def setUp(self):
        self.app = build_app(
            owner_email="owner@x.com", owner_password_hash=hash_password("SenhaForte123"), persistent=False,
        )
        self.atlas = self.app.atlas

    def tearDown(self):
        self.app.shutdown()

    def test_all_20_agents_present_as_attributes(self):
        for attr in (
            "metrica", "echo", "veritas", "juris", "prisma", "libra", "mercury", "nexus", "relay", "argos",
            "scriptor", "orion", "sage", "venture", "pulse", "aurora", "pixel", "frame", "vox", "sonora",
        ):
            self.assertTrue(hasattr(self.atlas, attr), f"AtlasDirector não tem o atributo '{attr}'")

    def test_sentinel_and_bridge_agents_present_and_reuse_real_instances(self):
        """MICROCORREÇÃO — SENTINEL/BRIDGE agora têm runtime real
        (`sentinel_agent`/`bridge_agent`), sem sobrescrever os
        atributos `sentinel`/`bridge` já existentes e ativamente
        usados em outras partes de `AtlasDirector`."""
        self.assertTrue(hasattr(self.atlas, "sentinel_agent"))
        self.assertTrue(hasattr(self.atlas, "bridge_agent"))
        self.assertIsNotNone(self.atlas.sentinel)  # preservado
        self.assertIsNotNone(self.atlas.bridge)  # preservado
        self.assertIs(self.atlas.bridge_agent.hub, self.atlas.bridge)
        self.assertIs(self.atlas.sentinel_agent.quality_supervisor, self.atlas.quality_supervisor)

    def test_all_25_agents_active_via_registry(self):
        from core.agents.base import ImplementationStatus

        active = self.atlas.agent_registry.by_implementation_status(ImplementationStatus.ACTIVE)
        self.assertEqual(len(active), 25)
        for status in (ImplementationStatus.PARTIAL, ImplementationStatus.SCAFFOLD):
            self.assertEqual(self.atlas.agent_registry.by_implementation_status(status), [])

    def test_metrica_uses_real_shared_finance_ledger(self):
        self.atlas.facade.finance.record(channel_id="c1", entry_type="revenue", amount=777.0, idempotency_key="k1")
        result = self.atlas.metrica.execute(task_type="COMPUTE_FINANCE_SNAPSHOT", payload={"channel_id": "c1"})
        self.assertEqual(result.status, AgentTaskStatus.COMPLETED)
        self.assertEqual(result.output["revenue"], 777.0)

    def test_nexus_uses_real_shared_crm(self):
        lead = self.atlas.facade.crm.create_lead("c1", idempotency_key="l1", source="x")
        result = self.atlas.nexus.execute(task_type="ANALYZE_FUNNEL_CONVERSION", payload={"channel_id": "c1"})
        self.assertEqual(result.status, AgentTaskStatus.COMPLETED)
        self.assertEqual(result.output["stage_totals"]["NEW"], 1)

    def test_libra_uses_real_shared_master_library(self):
        result = self.atlas.libra.execute(task_type="ADD_ENTRY", payload={
            "title": "t", "area": "a", "content": "c", "origin": "o", "responsible_agent": "LIBRA",
        })
        self.assertEqual(result.status, AgentTaskStatus.COMPLETED)
        # a mesma entrada deve ser visivel via o proprio self.library do AtlasDirector
        found = self.atlas.library.get(result.output["entry_id"])
        self.assertEqual(found.title, "t")

    def test_echo_persists_via_quantitative_learning_loop(self):
        r1 = self.atlas.echo.execute(task_type="RECORD_PREDICTION", payload={
            "predicting_agent_id": "ARGOS", "metric_name": "m", "predicted_value": 0.5, "context": "x",
            "scope_channel_id": "c1",
        })
        self.assertEqual(r1.status, AgentTaskStatus.COMPLETED)
        self.assertEqual(len(self.atlas.quantitative_learning_loop.all_predictions()), 1)

    def test_web_research_and_creative_agents_honestly_unavailable(self):
        for agent, task in [
            (self.atlas.orion, "SCAN_MARKET_OPPORTUNITIES"), (self.atlas.sage, "RESEARCH_AUDIENCE"),
            (self.atlas.venture, "DISCOVER_PRODUCTS"), (self.atlas.pulse, "CHECK_SEO_RANKING"),
            (self.atlas.aurora, "DEFINE_CREATIVE_DIRECTION"), (self.atlas.pixel, "GENERATE_THUMBNAIL"),
            (self.atlas.frame, "PRODUCE_VIDEO"), (self.atlas.vox, "GENERATE_NARRATION"),
            (self.atlas.sonora, "GENERATE_MUSIC"),
        ]:
            result = agent.execute(task_type=task, payload={"query": "x", "brief": "x"})
            self.assertEqual(result.status, AgentTaskStatus.UNAVAILABLE)

    def test_capabilities_registered_for_all_20_agents(self):
        for agent_id in (
            "METRICA", "ECHO", "VERITAS", "JURIS", "PRISMA", "LIBRA", "MERCURY", "NEXUS", "RELAY", "ARGOS",
            "SCRIPTOR", "ORION", "SAGE", "VENTURE", "PULSE", "AURORA", "PIXEL", "FRAME", "VOX", "SONORA",
        ):
            caps = self.atlas.capability_registry.capabilities_for(agent_id)
            self.assertGreater(len(caps), 0, f"nenhuma capacidade registrada para {agent_id}")


if __name__ == "__main__":
    unittest.main()
