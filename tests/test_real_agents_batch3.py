import unittest

from core.agents.runtime.argos import ArgosAgent
from core.agents.runtime.contracts import AgentTaskStatus
from core.agents.runtime.creative_agents import AuroraAgent, FrameAgent, PixelAgent, SonoraAgent, VoxAgent
from core.agents.runtime.providers import ProviderResponse
from core.agents.runtime.scriptor import ScriptorAgent
from core.agents.runtime.web_research_agents import OrionAgent, PulseAgent, SageAgent, VentureAgent
from core.learning.loop import LearningLoop, Prediction

"""
Testes BLOCO FINAL 1 (itens 1/2/3) — os 11 agentes restantes: pesquisa
web (dependente de provider, honesto UNAVAILABLE sem rede), criativos
multimodais (mesma disciplina), ARGOS (planejamento real via Learning
Loop), SCRIPTOR (montagem real de template).
"""


class FakeConfiguredProvider:
    """Provider de teste — simula uma integração real futura, usado
    SOMENTE para provar que a arquitetura funciona quando um provider
    de verdade está conectado (nunca usado como substituto de dado
    real em produção)."""

    def is_configured(self) -> bool:
        return True

    def call(self, *, operation: str, params: dict) -> ProviderResponse:
        return ProviderResponse(available=True, provider_name="fake_test_provider", data={"echo": params}, confidence=0.7)


class WebResearchAgentsTests(unittest.TestCase):
    def test_all_four_unavailable_without_provider(self):
        for cls, task in [
            (OrionAgent, "SCAN_MARKET_OPPORTUNITIES"), (SageAgent, "RESEARCH_AUDIENCE"),
            (VentureAgent, "DISCOVER_PRODUCTS"), (PulseAgent, "CHECK_SEO_RANKING"),
        ]:
            agent = cls()
            r = agent.execute(task_type=task, payload={"query": "x"})
            self.assertEqual(r.status, AgentTaskStatus.UNAVAILABLE, f"{cls.__name__} deveria ser UNAVAILABLE")
            self.assertEqual(r.confidence, 0.0)

    def test_works_when_real_provider_injected(self):
        agent = OrionAgent(provider=FakeConfiguredProvider())
        r = agent.execute(task_type="SCAN_MARKET_OPPORTUNITIES", payload={"query": "nicho x"})
        self.assertEqual(r.status, AgentTaskStatus.COMPLETED)
        self.assertEqual(r.output["echo"]["query"], "nicho x")

    def test_unknown_task_type_rejected(self):
        agent = SageAgent()
        r = agent.execute(task_type="TASK_INEXISTENTE", payload={})
        self.assertEqual(r.status, AgentTaskStatus.REJECTED)


class CreativeAgentsTests(unittest.TestCase):
    def test_all_five_unavailable_without_provider(self):
        for cls, task in [
            (AuroraAgent, "DEFINE_CREATIVE_DIRECTION"), (PixelAgent, "GENERATE_THUMBNAIL"),
            (FrameAgent, "PRODUCE_VIDEO"), (VoxAgent, "GENERATE_NARRATION"), (SonoraAgent, "GENERATE_MUSIC"),
        ]:
            agent = cls()
            r = agent.execute(task_type=task, payload={"brief": "x"})
            self.assertEqual(r.status, AgentTaskStatus.UNAVAILABLE, f"{cls.__name__} deveria ser UNAVAILABLE")

    def test_pixel_works_with_real_provider_injected(self):
        agent = PixelAgent(provider=FakeConfiguredProvider())
        r = agent.execute(task_type="GENERATE_THUMBNAIL", payload={"brief": "thumbnail para video X"})
        self.assertEqual(r.status, AgentTaskStatus.COMPLETED)


class ArgosAgentTests(unittest.TestCase):
    def test_never_fabricates_plan_without_topics(self):
        agent = ArgosAgent()
        r = agent.execute(task_type="BUILD_CONTENT_PLAN", payload={"candidate_topics": []})
        self.assertEqual(r.status, AgentTaskStatus.REJECTED)

    def test_plan_uses_real_validated_learnings(self):
        loop = LearningLoop()
        for _ in range(6):
            p = loop.record_prediction(Prediction.new(
                agent_id="ECHO", metric_name="m", predicted_value=0.5, context="x", scope_channel_id="c1",
            ))
            loop.record_outcome(prediction_id=p.prediction_id, actual_value=0.6)
        loop.promote_validated_learning(metric_name="m", statement="thumbnail vermelha converte", validated_by="ECHO", scope_channel_id="c1")
        agent = ArgosAgent(learning_loop=loop)
        r = agent.execute(task_type="BUILD_CONTENT_PLAN", payload={
            "candidate_topics": ["thumbnail vermelha para o proximo video", "topico aleatorio"],
            "scope_channel_id": "c1",
        })
        self.assertEqual(r.status, AgentTaskStatus.COMPLETED)
        self.assertGreater(r.output["ranked_topics"][0]["priority_score"], 0)

    def test_prioritize_topics_by_explicit_score(self):
        agent = ArgosAgent()
        r = agent.execute(task_type="PRIORITIZE_TOPICS", payload={"topics_with_scores": {"a": 1, "b": 5, "c": 3}})
        self.assertEqual(r.output["ranked"][0]["topic"], "b")


class ScriptorAgentTests(unittest.TestCase):
    def test_assembles_real_script_from_slots(self):
        agent = ScriptorAgent()
        r = agent.execute(task_type="ASSEMBLE_SCRIPT", payload={
            "template_id": "SHORT_HOOK_BODY_CTA", "slots": {"hook": "h", "body": "b", "cta": "c"},
        })
        self.assertEqual(r.status, AgentTaskStatus.COMPLETED)
        self.assertIn("h", r.output["script"])
        self.assertIn("b", r.output["script"])
        self.assertIn("c", r.output["script"])

    def test_unknown_template_rejected(self):
        agent = ScriptorAgent()
        r = agent.execute(task_type="ASSEMBLE_SCRIPT", payload={"template_id": "NAO_EXISTE", "slots": {}})
        self.assertEqual(r.status, AgentTaskStatus.REJECTED)

    def test_missing_required_slot_rejected_never_placeholder(self):
        agent = ScriptorAgent()
        r = agent.execute(task_type="ASSEMBLE_SCRIPT", payload={
            "template_id": "SHORT_HOOK_BODY_CTA", "slots": {"hook": "h"},
        })
        self.assertEqual(r.status, AgentTaskStatus.REJECTED)


if __name__ == "__main__":
    unittest.main()
