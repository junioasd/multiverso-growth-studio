"""
COMANDO MESTRE 5/6 — máquina comercial ponta a ponta.

LÓGICA VALIDADA LOCALMENTE pelo caminho real: `build_app` → Owner →
`GovernedFacade` (autorização + idempotência + auditoria) → SQLite →
Gêmeo; publicação via executor do Comando 3 contra SERVIDOR HTTP LOCAL REAL.
OPERAÇÃO EXTERNA REAL (gateways, plataformas, contas de anúncio) NÃO é
exercida aqui — nenhum resultado externo é afirmado.
"""
import http.server
import json
import os
import socketserver
import tempfile
import threading
import time
import unittest

from api.bootstrap import build_app
from atlas.learning import Recommendation, StrategyStatus
from commercial.claims import CommercialClaimRegistry
from commercial.journey import JourneyViolation, OrderJourney, distribution_ladder, unit_economics
from commercial.omnichannel_intelligence import ChannelType, OmnichannelIntelligence
from core.evidence.claims import ClaimType
from core.library.master_library import ConfidenceLevel, MasterLibrary
from core.models import db as db_module
from core.models.entities import ValueType
from core.security.auth import principal_from_session
from tests._auth_test_helpers import fast_hash_password

_PWD = "TestPassword123"
DAY = 24 * 3600.0


class _Base(unittest.TestCase):
    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db_module.remove_db_file(self.db)
        self.app = self._build(True)
        self.owner = principal_from_session(self.app.auth.current_session(self.app.auth.login("o@t.com", _PWD)))
        self.atlas = self.app.atlas
        self.j = OrderJourney(facade=self.app.facade, principal=self.owner, db_path=self.db)
        self._k = 0

    def tearDown(self):
        db_module.remove_db_file(self.db)

    def _build(self, init):
        return build_app(owner_email="o@t.com", owner_password_hash=fast_hash_password(_PWD), db_path=self.db,
                         init_database=init)

    def sql(self, q, p=()):
        with db_module.connect(self.db) as conn:
            return [dict(r) for r in conn.execute(q, p).fetchall()]

    def sales(self, event_type):
        return self.sql("SELECT * FROM sales_events WHERE event_type = ?", (event_type,))

    def ev(self, event_type, n, camp="camp_a", ch="chan_1", source=None):
        for _ in range(n):
            self._k += 1
            self.app.facade.record_sales_event(self.owner, channel_id=ch, event_type=event_type,
                                               value_type=ValueType.REAL, campaign_id=camp, traffic_source=source,
                                               idempotency_key=f"x{self._k}")

    def order(self, oid, amount=197.0, **kw):
        return self.j.start_checkout(order_id=oid, channel_id="chan_1", amount=amount, lead_id=kw.pop("lead", "lead_1"),
                                     product_id="prod_x", campaign_id="camp_a", traffic_source="organic", **kw)


# ------------------------------------------------------------- jornada
class JourneyTests(_Base):
    def test_checkout_is_not_a_sale_and_pending_is_not_payment(self):
        self.order("o1")
        self.j.payment_pending(order_id="o1", gateway_event_id="gw_pix_1")
        self.assertEqual(self.j.get("o1")["status"], "PAYMENT_PENDING")
        self.assertEqual(self.sales("sale"), [], "checkout/pendente NUNCA vira venda")
        self.assertEqual(len(self.sales("checkout")), 1)
        self.assertEqual(self.sql("SELECT * FROM finance_entries"), [], "sem pagamento, sem receita")

    def test_paid_requires_gateway_evidence_and_exact_amount(self):
        self.order("o1")
        with self.assertRaises(JourneyViolation):
            self.j.confirm_payment(order_id="o1", gateway_event_id="gw1", paid_amount=197.0, evidence="")
        with self.assertRaises(JourneyViolation):
            self.j.confirm_payment(order_id="o1", gateway_event_id="gw1", paid_amount=97.0, evidence="webhook gw")
        self.assertEqual(self.j.get("o1")["status"], "CHECKOUT_STARTED")
        self.j.confirm_payment(order_id="o1", gateway_event_id="gw1", paid_amount=197.0, evidence="webhook gw #1")
        self.assertEqual(self.j.get("o1")["status"], "PAID")
        self.assertEqual([s["amount"] for s in self.sales("sale")], [197.0])

    def test_payment_is_not_delivery_and_delivery_requires_payment(self):
        self.order("o1")
        with self.assertRaises(JourneyViolation):
            self.j.confirm_delivery(order_id="o1", delivery_event_id="d1", evidence="acesso liberado")
        self.j.confirm_payment(order_id="o1", gateway_event_id="gw1", paid_amount=197.0, evidence="gw")
        self.assertEqual(self.j.get("o1")["status"], "PAID", "pago ainda não é entregue")
        with self.assertRaises(JourneyViolation):
            self.j.confirm_delivery(order_id="o1", delivery_event_id="d1", evidence="")
        self.j.confirm_delivery(order_id="o1", delivery_event_id="d1", evidence="acesso ao curso liberado #99")
        self.assertEqual(self.j.get("o1")["status"], "DELIVERED")

    def test_retries_never_duplicate_sale_revenue_delivery_or_refund(self):
        self.order("o1", commission=40.0, product_cost=10.0)
        for _ in range(3):
            self.j.confirm_payment(order_id="o1", gateway_event_id="gw1", paid_amount=197.0, evidence="gw")
            self.j.confirm_delivery(order_id="o1", delivery_event_id="d1", evidence="ok")
            self.j.refund(order_id="o1", gateway_event_id="rf1", amount=197.0, reason="pedido do cliente")
            # checkout repetido: MESMO pedido com o MESMO payload (retry legítimo).
            # CORREÇÃO PÓS-AUDITORIA (D): antes o retry omitia comissão/custo e
            # passava só porque payload divergente era aceito em silêncio.
            self.order("o1", commission=40.0, product_cost=10.0)
        self.assertEqual(len(self.sales("sale")), 1)
        self.assertEqual(len(self.sales("refund")), 1)
        self.assertEqual(len(self.sales("checkout")), 1)
        types = sorted(r["entry_type"] for r in self.sql("SELECT entry_type FROM finance_entries"))
        self.assertEqual(types, ["commission", "realized_cost", "refund", "revenue"])

    def test_failed_payment_recovery_and_abandoned_cart(self):
        self.order("o_ab", now=time.time() - 7200)
        self.order("o_fail")
        self.j.fail_payment(order_id="o_fail", gateway_event_id="gwf", reason="cartão recusado")
        cands = {c["order_id"]: c["motivo"] for c in self.j.recovery_candidates()}
        self.assertEqual(cands, {"o_ab": "CARRINHO_ABANDONADO", "o_fail": "PAGAMENTO_FALHO"})
        self.j.confirm_payment(order_id="o_fail", gateway_event_id="gw_retry", paid_amount=197.0, evidence="2ª tentativa")
        self.assertEqual(self.j.get("o_fail")["status"], "PAID", "recuperação real")

    def test_post_sale_and_retention_only_with_real_repurchase(self):
        self.order("o1")
        self.j.confirm_payment(order_id="o1", gateway_event_id="g1", paid_amount=197.0, evidence="gw")
        self.j.confirm_delivery(order_id="o1", delivery_event_id="d1", evidence="ok")
        self.j.post_sale(order_id="o1", event_id="ps1", note="onboarding enviado")
        self.assertEqual(self.j.mark_retained(lead_id="lead_1"), [], "sem recompra, sem retenção")
        self.order("o2", amount=97.0)
        self.assertEqual(self.j.mark_retained(lead_id="lead_1"), [], "checkout de recompra não é recompra")
        self.j.confirm_payment(order_id="o2", gateway_event_id="g2", paid_amount=97.0, evidence="gw")
        self.assertEqual([o["status"] for o in self.j.mark_retained(lead_id="lead_1")], ["RETAINED"])

    def test_journey_state_survives_restart(self):
        self.order("o1")
        self.j.confirm_payment(order_id="o1", gateway_event_id="g1", paid_amount=197.0, evidence="gw")
        app2 = self._build(False)
        j2 = OrderJourney(facade=app2.facade, principal=self.owner, db_path=self.db)
        self.assertEqual(j2.get("o1")["status"], "PAID")
        j2.confirm_payment(order_id="o1", gateway_event_id="g1", paid_amount=197.0, evidence="gw")   # retry pós-restart
        self.assertEqual(len(self.sales("sale")), 1)


# ------------------------------------------------------------ finanças
class FinanceTests(_Base):
    def _paid(self, oid, amount, commission=0.0, cost=0.0):
        self.order(oid, amount=amount, commission=commission or None, product_cost=cost or None)
        self.j.confirm_payment(order_id=oid, gateway_event_id=f"g_{oid}", paid_amount=amount, evidence="gw")

    def test_refund_reduces_margin_revenue_is_not_profit(self):
        self._paid("o1", 200.0, commission=60.0, cost=20.0)
        self._paid("o2", 200.0, commission=60.0, cost=20.0)
        self.j.refund(order_id="o2", gateway_event_id="r2", amount=200.0, reason="chargeback")
        snap = self.app.atlas.facade.finance.snapshot("chan_1")
        self.assertEqual((snap.revenue, snap.refunds, snap.net_revenue), (400.0, 200.0, 200.0))
        self.assertEqual(snap.margin, 400.0 - 200.0 - 120.0 - 40.0)
        self.atlas.sync_digital_twin()
        self.assertEqual(self.atlas.twin_state("channel", "chan_1", "margin").fact_value, 40.0,
                         "margem do Gêmeo também desconta reembolso")

    def test_unit_economics_and_margin_destroying_revenue_is_not_success(self):
        self._paid("o1", 100.0, commission=50.0, cost=30.0)
        self._paid("o2", 100.0, commission=50.0, cost=30.0)
        self.app.facade.record_finance_entry(self.owner, entry_type="expense", amount=300.0, channel_id="chan_1",
                                             idempotency_key="ads_1")
        ue = unit_economics(finance_snapshot=self.atlas.facade.finance.snapshot("chan_1"), paid_orders=2, customers=2,
                            clicks=100, acquisition_spend=(300.0, "finance_entries: ads_1"))
        self.assertEqual(ue["gross_revenue"], 200.0)
        self.assertLess(ue["margin"], 0)
        self.assertEqual(ue["verdict"], "REVENUE_WITHOUT_MARGIN", "faturou 200, destruiu margem: NÃO é sucesso")
        self.assertEqual((ue["cac"], ue["conversion_rate"], ue["ltv_observed"]), (150.0, 0.02, 100.0))
        self.assertLess(ue["roi"], 0)

    def test_unknown_inputs_stay_unknown(self):
        ue = unit_economics(finance_snapshot=self.atlas.facade.finance.snapshot("vazio"), paid_orders=0, customers=0,
                            clicks=None, acquisition_spend=None)
        self.assertEqual((ue["cac"], ue["ltv_observed"], ue["conversion_rate"], ue["verdict"]),
                         ("UNKNOWN", "UNKNOWN", "UNKNOWN", "INSUFFICIENT_DATA"))
        with self.assertRaises(ValueError):
            unit_economics(finance_snapshot=self.atlas.facade.finance.snapshot("x"), paid_orders=1, customers=1,
                           clicks=1, acquisition_spend=(10.0, ""))

    def test_economic_success_when_margin_and_roi_positive(self):
        self._paid("o1", 300.0, commission=30.0, cost=20.0)
        ue = unit_economics(finance_snapshot=self.atlas.facade.finance.snapshot("chan_1"), paid_orders=1, customers=1,
                            clicks=20, acquisition_spend=None)
        self.assertEqual(ue["verdict"], "ECONOMIC_SUCCESS")


# ------------------------------------------------ atribuição / orgânico × pago
class AttributionTests(_Base):
    def test_paid_ptbr_markers_and_no_invented_attribution(self):
        o = OmnichannelIntelligence.__new__(OmnichannelIntelligence)
        cases = {"facebook_ads": ChannelType.PAID_ADS, "instagram_patrocinado": ChannelType.PAID_ADS,
                 "post_impulsionado": ChannelType.PAID_ADS, "trafego_pago": ChannelType.PAID_ADS,
                 "youtube_organic": ChannelType.ORGANIC, "busca_organico": ChannelType.ORGANIC,
                 "email": ChannelType.UNKNOWN, "direto": ChannelType.UNKNOWN, "indicacao": ChannelType.UNKNOWN,
                 "": ChannelType.UNKNOWN}
        for src, expected in cases.items():
            self.assertEqual(OmnichannelIntelligence.classify_traffic_source(o, src), expected, src)

    def test_sale_without_source_is_not_attributed(self):
        self.j.start_checkout(order_id="o1", channel_id="chan_1", amount=50.0, campaign_id="camp_a")
        self.j.confirm_payment(order_id="o1", gateway_event_id="g1", paid_amount=50.0, evidence="gw")
        self.atlas.sync_digital_twin()
        self.assertEqual(self.atlas.twin.keys(entity_type="traffic_source"), [],
                         "venda sem origem não recebe atribuição inventada")


# ------------------------------------------------------ claims / compliance
class ClaimsTests(_Base):
    def setUp(self):
        super().setUp()
        self.reg = CommercialClaimRegistry(twin=self.atlas.twin, db_path=self.db)
        now = time.time()
        self.fact = self.atlas.twin.record(
            entity_type="product", entity_id="prod_x", attribute="students_total", value=12000,
            claim_type=ClaimType.FACT, source_id="crm", source_ref="crm:count", observed_at=now,
            valid_until=now + DAY, confidence=0.95, quality=0.9, confirmed=True, recorded_by="t")
        self.hyp = self.atlas.twin.record(
            entity_type="product", entity_id="prod_x", attribute="weight_loss", value="5kg em 30 dias",
            claim_type=ClaimType.HYPOTHESIS, source_id="atlas", basis="palpite", observed_at=now,
            valid_until=now + DAY, confidence=0.4, quality=0.5, recorded_by="t")

    def test_evidenced_claim_approved_only_for_allowed_channel(self):
        c = self.reg.register(text="mais de 12 mil alunos", evidence_fact_ids=[self.fact.fact_id],
                              channels=["youtube"], owner="Owner", valid_until=time.time() + DAY)
        self.assertEqual(c["status"], "APPROVED")
        ok = self.reg.check_copy(copy_text="Junte-se a mais de 12 mil alunos", channel="youtube",
                                 claim_ids=[c["claim_id"]])
        self.assertEqual(ok["status"], "APPROVED")
        other = self.reg.check_copy(copy_text="Junte-se a mais de 12 mil alunos", channel="tiktok",
                                    claim_ids=[c["claim_id"]])
        self.assertEqual(other["status"], "BLOCKED")

    def test_hypothesis_forbidden_unregistered_and_expired_are_blocked(self):
        hyp = self.reg.register(text="perca 5kg em 30 dias", evidence_fact_ids=[self.hyp.fact_id],
                                channels=["youtube"], owner="Owner", valid_until=time.time() + DAY)
        self.assertEqual(hyp["status"], "BLOCKED")
        self.assertIn("hipótese", hyp["status_reason"])
        none = self.reg.register(text="o melhor do Brasil", evidence_fact_ids=[], channels=["youtube"], owner="O",
                                 valid_until=time.time() + DAY)
        self.assertEqual(none["status"], "BLOCKED")
        forb = self.reg.register(text="renda garantida todo mês", evidence_fact_ids=[self.fact.fact_id],
                                 channels=["youtube"], owner="O", valid_until=time.time() + DAY)
        self.assertEqual(forb["status"], "BLOCKED")
        chk = self.reg.check_copy(copy_text="Perca 5kg em 30 dias, resultado garantido!", channel="youtube",
                                  claim_ids=[])
        self.assertEqual(chk["status"], "BLOCKED")
        self.assertTrue(any("NÃO registrada" in p for p in chk["problems"]))
        c = self.reg.register(text="mais de 12 mil alunos", evidence_fact_ids=[self.fact.fact_id],
                              channels=["youtube"], owner="O", valid_until=time.time() + DAY)
        late = self.reg.check_copy(copy_text="mais de 12 mil alunos", channel="youtube", claim_ids=[c["claim_id"]],
                                   now=time.time() + 2 * DAY)
        self.assertEqual(late["status"], "BLOCKED", "claim/evidência vencidos não autorizam copy")


# ------------------------------------------------------ Biblioteca Mestre
class LibraryTests(_Base):
    def test_expired_and_contradictory_knowledge_never_governs(self):
        lib = MasterLibrary(db_path=self.db)
        now = time.time()
        a = lib.add(title="gancho de garantia", area="playbook", content="usar garantia no gancho", origin="ciclo",
                    responsible_agent="ATLAS", tags=["topic:gancho"], confidence=ConfidenceLevel.HIGH,
                    valid_until=now + DAY)
        lib.validate(a.entry_id, validated_by="Owner")
        self.assertEqual(lib.governing(area="playbook", topic="gancho")["status"], "GOVERNING")
        self.assertEqual(lib.governing(area="playbook", topic="gancho", now=now + 2 * DAY)["status"], "EXPIRED")
        b = lib.add(title="gancho de urgência", area="playbook", content="usar urgência no gancho", origin="radar",
                    responsible_agent="ATLAS", tags=["topic:gancho"], valid_until=now + DAY)
        lib.validate(b.entry_id, validated_by="Owner")
        self.assertEqual(lib.governing(area="playbook", topic="gancho")["status"], "CONFLICT")
        lib2 = MasterLibrary(db_path=self.db)                    # persistência da validade
        self.assertEqual(lib2.get(a.entry_id).valid_until, a.valid_until)


# ----------------------------------------------------------- distribuição
class _Svc(http.server.BaseHTTPRequestHandler):
    posts = []
    status = "published"

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        _Svc.posts.append(self.path)
        out = json.dumps({"id": f"vid_{len(_Svc.posts)}"}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def do_GET(self):
        out = json.dumps({"status": _Svc.status}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


class DistributionAndEndToEndTests(_Base):
    def setUp(self):
        super().setUp()
        from bridge.execution.platform_adapters import PlatformApiAdapter
        from bridge.execution.vault import TokenType
        from bridge.hub import ConnectorType, HealthStatus
        _Svc.posts, _Svc.status = [], "published"
        self.srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _Svc)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

        class YT(PlatformApiAdapter):
            platform = "youtube"
            base_url = f"http://127.0.0.1:{self.srv.server_address[1]}"

            def publish(self, **kw):
                return self._request(action="PUBLISH", method="POST", path="/publish", body=kw)

            def status(self, **kw):
                return self._request(action="STATUS", method="GET", path="/status", params=kw)

        ref = self.atlas.token_vault.store_token(platform="youtube", token_type=TokenType.OAUTH2_ACCESS_TOKEN, value="t")
        self.atlas.bridge.register_connector(platform="youtube", connector_type=ConnectorType.OFFICIAL_API,
                                             is_configured=True, authenticated=True, health=HealthStatus.OK,
                                             permissions_ok=True)
        self.atlas.navigator_real.register_adapter(platform="youtube", adapter=YT(vault=self.atlas.token_vault,
                                                                                  account_reference_id=ref))

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        super().tearDown()

    def publish(self, title="vídeo campanha"):
        from navigator.planning import OperationPlanner, OperationRunner, StepSpec
        runner = OperationRunner(db_path=self.db, planner=OperationPlanner(bridge_hub=self.atlas.bridge,
                                                                             evidence_gate=self.atlas.evidence_gate),
                                 navigator=self.atlas.navigator_real, escalation_desk=self.atlas.escalation_desk,
                                 actor="NAVIGATOR", observability=self.atlas.execution_observability)
        run_id = runner.create_run(objective="publicar", requested_by="ATLAS", steps=[StepSpec(
            step_id="pub", platform="youtube", action="PUBLISH_VIDEO", adapter_method="publish", params={"title": title},
            required_params=["title"], success_result_keys=["id"], verify_method="status",
            verify_param_from_result={"id": "id"})])
        status = runner.advance(run_id)
        self.assertEqual(status, "WAITING_APPROVAL", "publicação exige aprovação do Owner")
        self.assertEqual(_Svc.posts, [])
        _, st, _ = runner.load(run_id)
        self.atlas.escalation_desk.approve(st["pub"]["escalation_id"], decision="ok", resolved_by=self.owner)
        runner.advance(run_id)
        _, st, _ = runner.load(run_id)
        return st["pub"]["status"]

    def test_publication_needs_real_confirmation_and_ladder_uses_facts(self):
        _Svc.status = "processing"
        self.assertEqual(self.publish(), "EXECUTED", "enviado/aceito não é publicado")
        self.ev("impression", 1000)
        self.ev("view", 300)
        self.ev("click", 40)
        self.atlas.sync_digital_twin()
        lad = distribution_ladder(self.atlas.twin, campaign_id="camp_a", publication_confirmed=False)
        self.assertIsNone(lad["highest_confirmed_stage"], "sem confirmação real, nenhum degrau é afirmado")
        lad2 = distribution_ladder(self.atlas.twin, campaign_id="camp_a", publication_confirmed=True)
        self.assertEqual(lad2["highest_confirmed_stage"], "ENGAGED")
        self.assertEqual(lad2["stages"]["CONVERTED"], "DESCONHECIDO", "sem venda real: desconhecido, não zero")

    def test_end_to_end_radar_to_learning_and_recalibration(self):
        from atlas.learning import TestDesign
        # RADAR/ATLAS/Gêmeo: estratégia de campanha testada com resultado REAL
        self.ev("click", 100, camp="camp_t")
        self.ev("sale", 2, camp="camp_t")
        self.ev("click", 100, camp="camp_c")
        self.ev("sale", 2, camp="camp_c")
        self.atlas.sync_digital_twin()
        cy = self.atlas.adaptive_cycle
        s = cy.define_hypothesis(target_type="campaign", target_id="camp_t", metric_name="conversion_rate",
                                 hypothesis="gancho de garantia eleva conversão", expected_value=0.05,
                                 design=TestDesign.CONTROLLED, control_id="camp_c")
        # NAVIGATOR/FORGE/Owner/Hub: publicação confirmada
        self.assertEqual(self.publish("gancho de garantia"), "CONFIRMED")
        # lead/venda/pagamento/entrega reais da campanha
        for i in range(5):
            if i:
                cy.start_round(s.strategy_id)
            self.ev("click", 100, camp="camp_t")
            self.ev("click", 100, camp="camp_c")
            self.ev("sale", 2, camp="camp_c")
            for k in range(5):
                oid = f"o_{i}_{k}"
                self.j.start_checkout(order_id=oid, channel_id="chan_1", amount=100.0, campaign_id="camp_t",
                                      lead_id=f"l_{i}_{k}", commission=20.0, product_cost=10.0)
                self.j.confirm_payment(order_id=oid, gateway_event_id=f"g{oid}", paid_amount=100.0, evidence="gw")
                self.j.confirm_delivery(order_id=oid, delivery_event_id=f"d{oid}", evidence="acesso liberado")
            self.atlas.sync_digital_twin()
            cy.measure_round(s.strategy_id)
        # resultado financeiro medido
        ue = unit_economics(finance_snapshot=self.atlas.facade.finance.snapshot("chan_1"), paid_orders=25,
                            customers=25, clicks=500, acquisition_spend=None)
        self.assertEqual(ue["verdict"], "ECONOMIC_SUCCESS")
        self.assertEqual(cy.get(s.strategy_id).status, StrategyStatus.VALIDATED)
        # Biblioteca Mestre registra o aprendizado — com validade (reteste obrigatório)
        lib = MasterLibrary(db_path=self.db)
        e = lib.add(title=s.hypothesis, area="campaign_learnings", responsible_agent="ATLAS",
                    content=json.dumps({"strategy": s.strategy_id, "gancho": "garantia", "resultado": ue["verdict"],
                                        "margem": ue["margin"], "causal": cy.get(s.strategy_id).causal_status.value}),
                    origin=f"ciclo adaptativo {s.strategy_id}", tags=["topic:gancho_garantia"],
                    confidence=ConfidenceLevel.HIGH, valid_until=time.time() + 30 * DAY)
        lib.validate(e.entry_id, validated_by="Owner")
        self.assertEqual(lib.governing(area="campaign_learnings", topic="gancho_garantia")["status"], "GOVERNING")
        self.assertEqual(lib.governing(area="campaign_learnings", topic="gancho_garantia",
                                       now=time.time() + 31 * DAY)["status"], "EXPIRED",
                         "estratégia boa precisa ser retestada")
        self.assertEqual(cy.recommend(target_type="campaign", target_id="camp_t",
                                      metric_name="conversion_rate")["action"], Recommendation.SCALE)

    def test_bad_strategy_loses_priority_and_is_abandoned(self):
        from radar_web.intelligence import history_penalty_for
        self.ev("click", 100, camp="camp_r")
        self.ev("sale", 4, camp="camp_r")
        self.atlas.sync_digital_twin()
        cy = self.atlas.adaptive_cycle
        s = cy.define_hypothesis(target_type="campaign", target_id="camp_r", metric_name="conversion_rate",
                                 hypothesis="criativo escuro eleva conversão", expected_value=0.06)
        for i in range(5):
            if i:
                cy.start_round(s.strategy_id)
            self.ev("click", 100, camp="camp_r")
            self.ev("sale", 1, camp="camp_r")
            self.atlas.sync_digital_twin()
            cy.measure_round(s.strategy_id)
        self.assertEqual(cy.get(s.strategy_id).status, StrategyStatus.ABANDONED)
        self.assertEqual(cy.recommend(target_type="campaign", target_id="camp_r",
                                      metric_name="conversion_rate")["action"], Recommendation.ABANDON)
        self.assertGreater(history_penalty_for(cy, target_type="campaign", target_id="camp_r")[0], 0)


if __name__ == "__main__":
    unittest.main()
