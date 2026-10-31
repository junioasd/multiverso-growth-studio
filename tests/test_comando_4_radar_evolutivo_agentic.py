"""
COMANDO MESTRE 4/6 — RADAR WEB COMERCIAL + VISÃO EVOLUTIVA + MULTIMODAL +
ALL-ROUNDER + AGENTIC-FIRST.

Caminho real: `build_app` (Gêmeo, ciclo adaptativo, registro de agentes,
auditoria do diretor); páginas buscadas por `RealWebPageAnalyzer` num
servidor HTTP LOCAL REAL; imagem PNG real (Pillow) no `RealImageAnalyzer`;
agentes reais (PRISMA, SAGE). Dublês SÓ para agentes MAL-COMPORTADOS dos
testes adversariais (sem evidência, autoridade, laço, mutação).
LIMITE DECLARADO: nenhuma coleta de mercado EXTERNA ocorre aqui (sem rede).
"""
import http.server
import os
import socketserver
import tempfile
import threading
import time
import unittest

from api.bootstrap import build_app
from atlas.agentic import AgenticCoordinator
from atlas.digital_twin import ChangeKind, EvidenceRequirement, ReadingStatus, detect_changes
from atlas.learning import Recommendation, StrategyStatus, TestDesign
from core.agents.runtime.contracts import AgentTaskResult, RealAgentContract
from core.evidence.claims import ClaimType
from core.models import db as db_module
from core.models.entities import ValueType
from core.multimodal.commercial_signals import extract_commercial_signals, record_signals_in_twin
from core.multimodal.real_webpage_analyzer import RealWebPageAnalyzer
from core.security.auth import principal_from_session
from radar_web.intelligence import build_brief, history_penalty_for, prioritize
from tests._auth_test_helpers import fast_hash_password

_PWD = "TestPassword123"
DAY = 24 * 3600.0

PAGE_A = """<html><head><title>Emagreça 5kg em 30 dias com o Método X</title></head><body>
<h1>Método X</h1><p>Mais de 12 mil alunos. Garantia de 7 dias.</p>
<p>De graça: e-book bônus.</p><p>Por apenas R$ 97,00</p><a>Compre agora</a>
<p>Últimas vagas!</p></body></html>"""
PAGE_AMBIGUOUS = """<html><head><title>Curso Y</title></head><body>
<p>De R$ 497,00 por R$ 197,00 ou 12x de R$ 19,70</p></body></html>"""


class _Pages(http.server.BaseHTTPRequestHandler):
    pages: dict = {}

    def do_GET(self):
        body = self.pages.get(self.path, "<html><title>404</title></html>").encode("utf-8")
        self.send_response(200 if self.path in self.pages else 404)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


_REAL_URLOPEN = __import__("urllib.request", fromlist=["urlopen"]).urlopen


def _loopback_only_urlopen(request, *args, **kwargs):
    """CONTROLE de `urllib.request.urlopen` (exigido pela guarda
    `test_no_test_calls_network_methods_without_mocking_urlopen`): a busca
    continua REAL, mas só contra o servidor LOOPBACK que o próprio teste
    inicia — qualquer host externo é RECUSADO. Nenhum teste depende da
    rede/DNS do ambiente."""
    import urllib.parse
    url = getattr(request, "full_url", request)
    host = urllib.parse.urlparse(url).hostname
    if host not in ("127.0.0.1", "localhost"):
        raise AssertionError(f"rede EXTERNA proibida em teste: {host}")
    return _REAL_URLOPEN(request, *args, **kwargs)


class _Base(unittest.TestCase):
    def setUp(self):
        from unittest import mock
        self._net = mock.patch("urllib.request.urlopen", side_effect=_loopback_only_urlopen)
        self._net.start()
        self.addCleanup(self._net.stop)
        _Pages.pages = {"/a": PAGE_A, "/amb": PAGE_AMBIGUOUS}
        self.srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _Pages)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base_url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db_module.remove_db_file(self.db)
        self.app = self._build(True)
        self.owner = principal_from_session(self.app.auth.current_session(self.app.auth.login("o@t.com", _PWD)))
        self.atlas = self.app.atlas
        self._k = 0

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        db_module.remove_db_file(self.db)

    def _build(self, init):
        return build_app(owner_email="o@t.com", owner_password_hash=fast_hash_password(_PWD), db_path=self.db,
                         init_database=init)

    def fetch(self, path):
        return RealWebPageAnalyzer().analyze(reference=self.base_url + path)

    def observe(self, path, entity_id="concorrente_x", now=None, ttl=3 * DAY):
        ev = self.fetch(path)
        sigs = extract_commercial_signals(ev)
        record_signals_in_twin(self.atlas.twin, entity_type="competition", entity_id=entity_id, signals=sigs,
                               ttl_seconds=ttl, recorded_by="RADAR", now=now)
        return ev, sigs

    def ev(self, camp, clicks, sales):
        for _ in range(clicks):
            self._k += 1
            self.app.facade.record_sales_event(self.owner, channel_id="c", event_type="click", value_type=ValueType.REAL,
                                               campaign_id=camp, idempotency_key=f"e{self._k}")
        for _ in range(sales):
            self._k += 1
            self.app.facade.record_sales_event(self.owner, channel_id="c", event_type="sale", value_type=ValueType.REAL,
                                               amount=100.0, campaign_id=camp, idempotency_key=f"e{self._k}")


# ------------------------------------------------ proveniência / freshness
class ProvenanceFreshnessTests(_Base):
    def test_real_fetch_signals_are_traceable_to_original_evidence(self):
        ev, sigs = self.observe("/a")
        self.assertTrue(ev.reference_hash)
        for s in sigs:
            if s.claim_type == ClaimType.FACT:
                self.assertEqual(ev.content_text[s.span[0]:s.span[1]], s.quote, "trecho literal exato")
                self.assertEqual((s.evidence_id, s.reference_hash), (ev.evidence_id, ev.reference_hash))
        rd = self.atlas.twin_state("competition", "concorrente_x", "signal.cta_phrase")
        self.assertEqual(rd.status, ReadingStatus.CURRENT)
        f = rd.facts[0]
        self.assertEqual(f.source_id, f"multimodal:{self.base_url}/a")
        self.assertIn(ev.evidence_id, f.source_ref)
        self.assertIn(ev.reference_hash, f.source_ref)

    def test_expired_market_data_never_governs_decision(self):
        self.observe("/a")
        later = time.time() + 3 * DAY + 60
        rd = self.atlas.twin.reading("competition", "concorrente_x", "single_listed_amount", now=later)
        self.assertEqual(rd.status, ReadingStatus.EXPIRED)
        self.assertFalse(self.atlas.evidence_gate.assess(
            [EvidenceRequirement("competition", "concorrente_x", "single_listed_amount")], now=later).allowed)

    def test_absence_is_unknown_not_invented(self):
        brief = build_brief(self.atlas.twin, opportunity_id="op_vazia", entity_type="competition",
                            entity_id="sem_dados", context={"canal": "youtube"})
        self.assertEqual(brief.status, "INSUFFICIENT_EVIDENCE")
        self.assertIn("publico", brief.fields["dados_faltantes"])
        self.assertEqual(self.atlas.twin_state("competition", "sem_dados", "single_listed_amount").status,
                         ReadingStatus.UNKNOWN)

    def test_weak_source_does_not_become_decision_grade_fact(self):
        now = time.time()
        self.atlas.twin.record(entity_type="competition", entity_id="boato", attribute="signal.market_shift",
                               value="plataforma vai banir afiliados", claim_type=ClaimType.FACT,
                               source_id="discovery:blog_anonimo", source_ref="post_123", observed_at=now,
                               valid_until=now + DAY, confidence=0.2, quality=0.2, confirmed=False, recorded_by="RADAR")
        rd = self.atlas.twin_state("competition", "boato", "signal.market_shift")
        self.assertEqual(rd.status, ReadingStatus.LOW_CONFIDENCE)
        self.assertFalse(rd.usable_for_decision)


# ------------------------------------------ Visão Evolutiva / mudança real
class EvolutionTests(_Base):
    def test_persistent_transient_and_unconfirmed_changes(self):
        t0 = time.time()
        _Pages.pages["/p"] = PAGE_A
        self.observe("/p", entity_id="px", now=t0)
        _Pages.pages["/p"] = PAGE_A.replace("R$ 97,00", "R$ 147,00")
        self.observe("/p", entity_id="px", now=t0 + 10)
        changes = detect_changes(self.atlas.twin, "competition", "px", "single_listed_amount")
        self.assertEqual([(c.from_value, c.to_value, c.kind) for c in changes],
                         [(97.0, 147.0, ChangeKind.UNCONFIRMED)], "tendência sem sustentação NÃO é mudança real")
        self.observe("/p", entity_id="px", now=t0 + 20)          # mesmo valor: sem versão nova
        _Pages.pages["/p"] = PAGE_A.replace("R$ 97,00", "R$ 149,00")
        self.observe("/p", entity_id="px", now=t0 + 30)
        c = detect_changes(self.atlas.twin, "competition", "px", "single_listed_amount")[0]
        self.assertEqual((c.kind, c.from_value, c.to_value), (ChangeKind.PERSISTENT, 97.0, 147.0))
        self.assertAlmostEqual(c.changed_at, t0 + 10, places=3)
        self.assertEqual(c.source_id, f"multimodal:{self.base_url}/p")
        self.assertIn("causalidade NÃO demonstrada", c.note)
        _Pages.pages["/q"] = PAGE_A
        self.observe("/q", entity_id="qx", now=t0)
        _Pages.pages["/q"] = PAGE_A.replace("R$ 97,00", "R$ 197,00")
        self.observe("/q", entity_id="qx", now=t0 + 10)
        _Pages.pages["/q"] = PAGE_A
        self.observe("/q", entity_id="qx", now=t0 + 20)
        self.assertEqual(detect_changes(self.atlas.twin, "competition", "qx", "single_listed_amount")[0].kind,
                         ChangeKind.TRANSIENT, "pontual: voltou ao nível anterior")

    def test_inference_never_produces_market_change(self):
        now = time.time()
        for i, v in enumerate((0.1, 0.9)):
            self.atlas.twin.record(entity_type="competition", entity_id="i", attribute="demand", value=v,
                                   claim_type=ClaimType.INFERENCE, source_id="modelo", basis="estimativa",
                                   observed_at=now + i, valid_until=now + DAY, confidence=0.5, quality=0.5,
                                   recorded_by="t", now=now + i)
        self.assertEqual(detect_changes(self.atlas.twin, "competition", "i", "demand"), [])


# ------------------------------------------------------------- conflito
class ConflictTests(_Base):
    def test_conflicting_sources_block_opportunity_and_critical_decision(self):
        _Pages.pages["/b"] = PAGE_A.replace("R$ 97,00", "R$ 127,00")
        self.observe("/a", entity_id="oferta_z")
        self.observe("/b", entity_id="oferta_z")
        rd = self.atlas.twin_state("competition", "oferta_z", "single_listed_amount")
        self.assertEqual(rd.status, ReadingStatus.CONFLICT)
        brief = build_brief(self.atlas.twin, opportunity_id="op_z", entity_type="competition", entity_id="oferta_z",
                            context={})
        self.assertEqual(brief.status, "BLOCKED_CONFLICT")
        ranking = prioritize([{"brief": brief, "criteria": {}}])
        self.assertEqual(ranking["ranked"], [])
        self.assertEqual(ranking["not_ranked"][0]["status"], "BLOCKED_CONFLICT")
        d = self.atlas.decide_with_evidence(topic="precificar", scope="competition:oferta_z", chosen="cobrir preço",
                                            justification="x", requirements=[EvidenceRequirement(
                                                "competition", "oferta_z", "single_listed_amount")])
        self.assertEqual(d.status.value, "BLOCKED")


# ------------------------------------------------------------ multimodal
class MultimodalTests(_Base):
    def test_signals_classified_literal_fact_vs_interpretation(self):
        _, sigs = self.observe("/a")
        by = {}
        for s in sigs:
            by.setdefault(s.name, []).append(s)
        self.assertEqual(by["cta_phrase"][0].claim_type, ClaimType.FACT)
        self.assertEqual(by["guarantee_mention"][0].quote, "Garantia de 7 dias")
        self.assertEqual(by["promise"][0].claim_type, ClaimType.INFERENCE)
        self.assertEqual((by["offer_price"][0].claim_type, by["offer_price"][0].value), (ClaimType.INFERENCE, 97.0))
        self.assertEqual(by["sales_angle"][0].claim_type, ClaimType.INFERENCE)
        self.assertEqual(by["message_offer_inconsistency"][0].claim_type, ClaimType.INFERENCE)
        promise = self.atlas.twin_state("competition", "concorrente_x", "signal.promise")
        self.assertEqual(promise.status, ReadingStatus.NOT_OBSERVED, "interpretação nunca vira fato no Gêmeo")

    def test_ambiguous_content_never_becomes_fact(self):
        _, sigs = self.observe("/amb", entity_id="amb")
        price = [s for s in sigs if s.name == "offer_price"][0]
        self.assertEqual((price.claim_type, price.value), (ClaimType.UNKNOWN, None))
        self.assertIn("AMBÍGUO", price.note)
        self.assertEqual(self.atlas.twin_state("competition", "amb", "signal.offer_price").status,
                         ReadingStatus.UNKNOWN)
        self.assertEqual(self.atlas.twin_state("competition", "amb", "single_listed_amount").status,
                         ReadingStatus.UNKNOWN)

    def test_image_semantics_unknown_without_vision_model(self):
        from PIL import Image
        from core.multimodal.real_image_analyzer import RealImageAnalyzer
        fd, png = tempfile.mkstemp(suffix=".png")
        os.close(fd)
        try:
            Image.new("RGB", (1280, 720), (200, 30, 30)).save(png)
            ev = RealImageAnalyzer().analyze(reference=png)
            self.assertEqual(ev.claim_type, ClaimType.FACT, "propriedades medidas da imagem são fato")
            sigs = extract_commercial_signals(ev)
            self.assertEqual([(s.name, s.claim_type) for s in sigs], [("visual_semantics", ClaimType.UNKNOWN)])
        finally:
            os.remove(png)

    def test_external_network_is_refused_by_the_test_control(self):
        ev = RealWebPageAnalyzer().analyze(reference="https://exemplo-externo.invalid/pagina")
        self.assertEqual(ev.confidence, 0.0)
        self.assertIn("rede EXTERNA proibida", ev.summary)

    def test_unavailable_source_yields_unknown(self):
        ev = RealWebPageAnalyzer().analyze(reference="http://127.0.0.1:9/nada", context={"timeout_seconds": 1})
        self.assertEqual(extract_commercial_signals(ev)[0].claim_type, ClaimType.UNKNOWN)


# ------------------------------------------------------- agentic-first
class _BadAgent(RealAgentContract):
    """Dublê de agente MAL-COMPORTADO (adversarial) — nunca usado como prova de sucesso."""

    def __init__(self, agent_id, task_types, behavior):
        super().__init__()
        self.agent_id, self.primary_task_types, self.behavior = agent_id, frozenset(task_types), behavior
        self.seen_payloads = []

    def execute_real(self, *, task_type, payload):
        self.seen_payloads.append(dict(payload))
        payload["contaminado"] = True
        return self.behavior(self, task_type, payload)


class _Reg:
    def __init__(self, real, extra):
        self.real, self.extra = real, {a.agent_id: a for a in extra}

    def all(self):
        return [type("R", (), {"agent_id": a, "contract": c})() for a, c in self.extra.items()] + list(self.real.all())

    def get(self, agent_id):
        if agent_id in self.extra:
            return type("R", (), {"agent_id": agent_id, "contract": self.extra[agent_id]})()
        return self.real.get(agent_id)


class AgenticTests(_Base):
    def coord(self, extra=(), **kw):
        return AgenticCoordinator(registry=_Reg(self.atlas.agent_registry, extra), audit=self.app.facade.audit,
                                  actor="ATLAS", **kw)

    def test_specialist_selected_and_result_is_inference(self):
        r = self.atlas.agentic.delegate(objective="série de conversão", task_type="DESCRIBE_SERIES",
                                        payload={"values": [0.02, 0.03, 0.05]}, evidence_refs=["tf_1"])
        self.assertEqual((r.status, r.agent_id, r.claim_type), ("ACCEPTED", "PRISMA", ClaimType.INFERENCE))
        self.assertEqual(r.attempts[0].tier, "SPECIALIST")

    def test_result_without_evidence_rejected_and_redelegated(self):
        bad = _BadAgent("FAKE_NO_EVIDENCE", {"DESCRIBE_SERIES"},
                        lambda s, t, p: AgentTaskResult.completed(agent_id=s.agent_id, task_type=t, output={"mean": 999}))
        r = self.coord([bad]).delegate(objective="o", task_type="DESCRIBE_SERIES", payload={"values": [1.0, 2.0, 3.0]},
                                       preferred_agent="FAKE_NO_EVIDENCE")
        self.assertEqual(r.status, "ESCALATED", "sem evidência de entrada, nem o real é aceito")
        self.assertIn("SEM evidência", r.attempts[0].reason)
        r2 = self.coord([bad]).delegate(objective="o", task_type="DESCRIBE_SERIES", payload={"values": [1.0, 2.0, 3.0]},
                                        preferred_agent="FAKE_NO_EVIDENCE", evidence_refs=["tf_serie"],
                                        required_output_keys=["median"])
        self.assertEqual([a.agent_id for a in r2.attempts], ["FAKE_NO_EVIDENCE", "PRISMA"])
        self.assertEqual((r2.status, r2.agent_id, r2.output["mean"]), ("ACCEPTED", "PRISMA", 2.0))

    def test_insufficient_data_inside_completed_is_rejected(self):
        r = self.atlas.agentic.delegate(objective="o", task_type="DESCRIBE_SERIES", payload={"values": [1.0, 2.0]},
                                        evidence_refs=["tf"])
        self.assertNotEqual(r.status, "ACCEPTED")
        self.assertIn("INSUFFICIENT_DATA", r.attempts[0].reason)

    def test_isolation_between_attempts(self):
        bad = _BadAgent("FAKE_MUTATOR", {"DESCRIBE_SERIES"},
                        lambda s, t, p: AgentTaskResult.failed(agent_id=s.agent_id, task_type=t, reason="x"))
        payload = {"values": [1.0, 2.0, 3.0]}
        r = self.coord([bad]).delegate(objective="o", task_type="DESCRIBE_SERIES", payload=payload,
                                       evidence_refs=["tf"], preferred_agent="FAKE_MUTATOR")
        self.assertEqual(r.status, "ACCEPTED")
        self.assertNotIn("contaminado", payload, "tentativa não contamina o chamador nem a próxima")

    def test_agent_cannot_exceed_owner_authority(self):
        bad = _BadAgent("FAKE_USURPER", {"DESCRIBE_SERIES"}, lambda s, t, p: AgentTaskResult.completed(
            agent_id=s.agent_id, task_type=t, output={"owner_decision": "aprovado", "approve": True},
            evidence_ids=["x"], confidence=0.9))
        r = self.coord([bad], max_attempts=1).delegate(objective="o", task_type="DESCRIBE_SERIES",
                                                       payload={"values": [1.0]}, preferred_agent="FAKE_USURPER")
        self.assertNotEqual(r.status, "ACCEPTED")
        self.assertIn("autoridade", r.attempts[0].reason)
        from core.security.auth import Principal, Role
        esc = self.atlas.request_critical_action(action_type="publish_content", details={"x": 1})
        with self.assertRaises(Exception):
            self.atlas.escalation_desk.approve(esc.escalation_id, decision="ok",
                                               resolved_by=Principal(subject="agente", role=Role.ATLAS, scopes={"*"}))
        self.assertNotEqual(self.atlas.escalation_desk.get(esc.escalation_id).status.value, "APPROVED")

    def test_loop_and_depth_are_blocked(self):
        looping = _BadAgent("FAKE_LOOP", {"LOOP_TASK"}, lambda s, t, p: AgentTaskResult.completed(
            agent_id=s.agent_id, task_type=t, output={"delegate_request": {"task_type": "LOOP_TASK",
                                                                             "payload": {"k": 1}}},
            evidence_ids=["e"], confidence=0.8))
        r = self.coord([looping]).delegate(objective="o", task_type="LOOP_TASK", payload={"k": 1})
        self.assertNotEqual(r.status, "ACCEPTED")
        self.assertEqual(r.sub_results[0].status, "LOOP_BLOCKED")
        self.assertLessEqual(len(looping.seen_payloads), 2, "sem recursão infinita")

    def test_attempt_and_time_budgets(self):
        failing = [_BadAgent(f"FAKE_FAIL_{i}", {"DESCRIBE_SERIES"}, lambda s, t, p: AgentTaskResult.failed(
            agent_id=s.agent_id, task_type=t, reason="erro")) for i in range(3)]
        r = self.coord(failing, max_attempts=2).delegate(objective="o", task_type="DESCRIBE_SERIES",
                                                         payload={"values": [1.0]}, evidence_refs=["e"])
        self.assertEqual((r.status, len(r.attempts)), ("BUDGET_EXCEEDED", 2))
        slow = _BadAgent("FAKE_SLOW", {"DESCRIBE_SERIES"}, lambda s, t, p: (time.sleep(0.3), AgentTaskResult.failed(
            agent_id=s.agent_id, task_type=t, reason="lento"))[1])
        r2 = self.coord([slow], time_budget_seconds=0.1).delegate(objective="o", task_type="DESCRIBE_SERIES",
                                                                  payload={"values": [1.0]}, evidence_refs=["e"])
        self.assertEqual(r2.status, "BUDGET_EXCEEDED")
        self.assertIn("tempo", r2.reason)

    def test_coordination_is_audited(self):
        self.atlas.agentic.delegate(objective="o", task_type="DESCRIBE_SERIES", payload={"values": [1.0]},
                                    evidence_refs=["e"])
        actions = [e.action for e in self.app.facade.audit.all()]
        self.assertIn("atlas_agentic_attempt", actions)
        self.assertIn("atlas_agentic_coordination", actions)


class AllRounderTests(_Base):
    def test_integrates_perspectives_and_exposes_missing_ones(self):
        out = self.atlas.agentic.integrate(objective="avaliar campanha", perspectives={
            "desempenho": {"task_type": "DESCRIBE_SERIES", "payload": {"values": [0.02, 0.03, 0.04]},
                           "evidence_refs": ["tf_a"]},
            "relacao": {"task_type": "CORRELATE_SERIES", "payload": {"series_a": [1, 2, 3], "series_b": [2, 4, 7]},
                        "evidence_refs": ["tf_b"]},
            "publico": {"task_type": "RESEARCH_AUDIENCE", "payload": {"niche": "fitness"}},
        })
        self.assertFalse(out["complete"])
        self.assertEqual(set(out["perspectives"]), {"desempenho", "relacao"})
        self.assertEqual(out["perspectives"]["desempenho"]["agent"], "PRISMA")
        self.assertIn("publico", out["missing_perspectives"])
        self.assertEqual(out["claim_type"], "INFERENCE", "correlação integrada nunca vira causa/fato")


# ---------------------------------- oportunidade / priorização / adaptação
class OpportunityAndAdaptationTests(_Base):
    def test_brief_has_all_fields_never_validated_and_explained_ranking(self):
        self.observe("/a", entity_id="op_a")
        _Pages.pages["/c"] = PAGE_A.replace("Compre agora", "Inscreva-se")
        self.observe("/c", entity_id="op_c")
        ctx = {"mercado": "emagrecimento", "publico": "mulheres 30-45", "canal": "youtube",
               "hipotese": "ângulo de garantia converte melhor", "metrica_sucesso": "conversion_rate",
               "prazo_teste_dias": 14, "criterio_abandono": "CONTRADICTED no ciclo adaptativo"}
        a = build_brief(self.atlas.twin, opportunity_id="op_a", entity_type="competition", entity_id="op_a", context=ctx)
        c = build_brief(self.atlas.twin, opportunity_id="op_c", entity_type="competition", entity_id="op_c", context={})
        self.assertEqual((a.status, c.status), ("CANDIDATE", "CANDIDATE"))
        self.assertNotIn("VALIDATED", (a.status, c.status))
        self.assertIn("custo_estimado", a.fields["dados_faltantes"])
        crit = {"evidence_strength": (0.8, "Gêmeo: fatos CURRENT confirmados"),
                "economic_potential": (0.6, "declarado pelo Owner"), "cost": (0.3, "orçamento do teste")}
        rk = prioritize([{"brief": a, "criteria": crit}, {"brief": c, "criteria": {
            "evidence_strength": (0.4, "Gêmeo"), "economic_potential": (0.6, "Owner"), "cost": (0.3, "orçamento")}}])
        self.assertEqual([r["opportunity"] for r in rk["ranked"]], ["op_a", "op_c"])
        self.assertEqual(rk["ranked"][0]["por_que"]["evidence_strength"]["origem"], "Gêmeo: fatos CURRENT confirmados")
        self.assertIn("margin", rk["ranked"][0]["criterios_ausentes"])
        with self.assertRaises(ValueError):
            prioritize([{"brief": a, "criteria": {"risk": (0.5, "")}}])

    def _validated_strategy(self):
        self.ev("t", 100, 2)
        self.ev("k", 100, 2)
        self.atlas.sync_digital_twin()
        cy = self.atlas.adaptive_cycle
        s = cy.define_hypothesis(target_type="campaign", target_id="t", metric_name="conversion_rate",
                                 hypothesis="gancho eleva conversão", expected_value=0.05, design=TestDesign.CONTROLLED,
                                 control_id="k")
        for i in range(5):
            if i:
                cy.start_round(s.strategy_id)
            self.ev("t", 100, 5)
            self.ev("k", 100, 2)
            self.atlas.sync_digital_twin()
            cy.measure_round(s.strategy_id)
        return cy, s

    def test_regime_change_invalidates_old_validation_and_survives_restart(self):
        cy, s = self._validated_strategy()
        rec = lambda c: c.recommend(target_type="campaign", target_id="t", metric_name="conversion_rate")["action"]
        self.assertEqual(rec(cy), Recommendation.SCALE)
        self.ev("t", 1500, 3)
        self.atlas.sync_digital_twin()
        self.assertEqual(rec(cy), Recommendation.HOLD, "uma observação: suspende, mas não declara mudança")
        self.assertEqual(cy.get(s.strategy_id).status, StrategyStatus.VALIDATED)
        self.ev("t", 1500, 3)
        self.atlas.sync_digital_twin()
        self.assertEqual(rec(cy), Recommendation.REVALIDATE)
        self.assertEqual(cy.get(s.strategy_id).status, StrategyStatus.NEEDS_REVALIDATION)
        app2 = self._build(False)                                  # restart
        self.assertEqual(app2.atlas.adaptive_cycle.get(s.strategy_id).status, StrategyStatus.NEEDS_REVALIDATION)
        self.assertEqual(rec(app2.atlas.adaptive_cycle), Recommendation.REVALIDATE)
        pen = history_penalty_for(app2.atlas.adaptive_cycle, target_type="campaign", target_id="t")
        self.assertIsNotNone(pen)

    def test_refuted_hypothesis_is_abandoned_not_recommended_and_deprioritized(self):
        self.ev("r", 100, 4)
        self.atlas.sync_digital_twin()
        cy = self.atlas.adaptive_cycle
        s = cy.define_hypothesis(target_type="campaign", target_id="r", metric_name="conversion_rate",
                                 hypothesis="thumbnail escura eleva conversão", expected_value=0.06)
        for i in range(5):
            if i:
                cy.start_round(s.strategy_id)
            self.ev("r", 100, 1)
            self.atlas.sync_digital_twin()
            cy.measure_round(s.strategy_id)
        self.assertEqual(cy.recommend(target_type="campaign", target_id="r", metric_name="conversion_rate")["action"],
                         Recommendation.ABANDON)
        value, origin = history_penalty_for(cy, target_type="campaign", target_id="r")
        self.assertGreater(value, 0)
        self.assertIn(s.strategy_id, origin)


# --------------------------- integração RADAR → ATLAS → Gêmeo → NAVIGATOR
class RadarToNavigatorIntegrationTests(_Base):
    def test_radar_evidence_gates_navigator_plan(self):
        from bridge.hub import ConnectorType, HealthStatus
        from navigator.planning import OperationPlanner, StepSpec
        self.observe("/a", entity_id="op_nav")
        brief = build_brief(self.atlas.twin, opportunity_id="op_nav", entity_type="competition", entity_id="op_nav",
                            context={"canal": "youtube"})
        self.assertEqual(brief.status, "CANDIDATE")
        self.atlas.bridge.register_connector(platform="youtube", connector_type=ConnectorType.OFFICIAL_API,
                                             is_configured=True, authenticated=True, health=HealthStatus.OK,
                                             permissions_ok=True)
        step = StepSpec(step_id="criativo", platform="youtube", action="CREATE_ASSET", adapter_method="create_asset",
                        params={"title": "resposta à oferta concorrente"}, required_params=["title"],
                        preconditions=[("competition", "op_nav", "single_listed_amount")])
        planner = OperationPlanner(bridge_hub=self.atlas.bridge, evidence_gate=self.atlas.evidence_gate)
        self.assertEqual(planner.plan(objective="responder oferta", steps=[step])["blocked"], [])
        _Pages.pages["/a"] = PAGE_AMBIGUOUS                     # o mercado mudou: preço agora ambíguo
        self.observe("/a", entity_id="op_nav2")
        step2 = StepSpec(**{**step.__dict__, "preconditions": [("competition", "op_nav2", "single_listed_amount")]})
        blocked = planner.plan(objective="responder oferta", steps=[step2])
        self.assertEqual(blocked["blocked"], ["criativo"], "sem fato vigente, o NAVIGATOR não executa")


class DirectorRadarEntryPointTests(_Base):
    def test_director_observation_is_audited_and_feeds_twin(self):
        out = self.atlas.radar_observe_page(url=self.base_url + "/a", entity_id="via_diretor")
        self.assertTrue(out["recorded"])
        self.assertEqual(self.atlas.twin_state("competition", "via_diretor", "single_listed_amount").fact_value, 97.0)
        ev = [e for e in self.app.facade.audit.all() if e.action == "atlas_radar_observe_page"][-1]
        self.assertEqual((ev.outcome, ev.details["evidence_id"]), ("SUCCESS", out["evidence"].evidence_id))
        down = self.atlas.radar_observe_page(url="http://127.0.0.1:9/x", entity_id="fora_do_ar")
        self.assertEqual(down["recorded"], [], "fonte indisponível não grava nada")
        self.assertEqual(self.atlas.twin_state("competition", "fora_do_ar", "single_listed_amount").status,
                         ReadingStatus.UNKNOWN)


class ContinuousRadarTests(_Base):
    def test_queue_driven_observation_builds_real_history_and_detects_change(self):
        from core.jobs.queue import JobQueue
        from radar_web.intelligence import process_observation_jobs, schedule_observation
        queue = JobQueue(db_path=self.db)
        _Pages.pages["/m"] = PAGE_A
        schedule_observation(queue, url=self.base_url + "/m", entity_id="monitorado", tick=1)
        self.assertEqual(process_observation_jobs(queue, self.atlas)[0][0], "monitorado")
        for price in ("R$ 147,00", "R$ 149,00"):          # o mercado muda entre ticks
            time.sleep(0.01)
            _Pages.pages["/m"] = PAGE_A.replace("R$ 97,00", price)
            self.assertEqual(len(process_observation_jobs(queue, self.atlas)), 1, "um tick por vez, reagendado")
        c = detect_changes(self.atlas.twin, "competition", "monitorado", "single_listed_amount")[0]
        self.assertEqual((c.from_value, c.to_value, c.kind), (97.0, 147.0, ChangeKind.PERSISTENT))
        pending = [j for j in queue.all() if j.job_type == "radar_observe_page" and j.state.value == "QUEUED"]
        self.assertEqual([j.payload["tick"] for j in pending], [4], "monitoramento contínuo segue agendado")
        _Pages.pages.pop("/m")                          # fonte sai do ar
        process_observation_jobs(queue, self.atlas)
        self.assertEqual(self.atlas.twin_state("competition", "monitorado", "single_listed_amount").fact_value, 149.0,
                         "indisponibilidade não apaga nem inventa estado")


if __name__ == "__main__":
    unittest.main()
