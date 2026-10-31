"""
COMANDO MESTRE 2/6 — ATLAS + GÊMEO DIGITAL ADAPTATIVO.

Todos os dados entram pelo CAMINHO REAL: `api.bootstrap.build_app` →
login do Owner → `GovernedFacade` → SQLite → `AtlasDirector.sync_digital_twin`.
Nenhum mock de dado. O único parâmetro injetado é o relógio (`now`) para
avaliar EXPIRAÇÃO sem esperar horas — os fatos avaliados são reais.
"""
import json
import os
import sqlite3
import tempfile
import time
import unittest

from api.bootstrap import build_app
from atlas.digital_twin import (
    TTL_PERFORMANCE, DecisionLedger, DecisionStatus, DigitalTwin, EvidenceRequirement, ReadingStatus,
    TwinValidationError,
)
from atlas.learning import CausalStatus, InsufficientEvidence, Recommendation, StrategyStatus, TestDesign
from core.evidence.claims import ClaimType
from core.learning.loop import LearningLoop, Prediction, ValidationStatus
from core.models import db as db_module
from core.models.entities import ValueType
from core.security.auth import principal_from_session
from tests._auth_test_helpers import fast_hash_password

_PWD = "TestPassword123"


class _RealAppBase(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db_module.remove_db_file(self.db_path)
        self.app = self._build(init=True)
        self.owner = principal_from_session(self.app.auth.current_session(self.app.auth.login("o@t.com", _PWD)))
        self.atlas = self.app.atlas
        self._k = 0

    def tearDown(self):
        db_module.remove_db_file(self.db_path)

    def _build(self, *, init: bool):
        return build_app(owner_email="o@t.com", owner_password_hash=fast_hash_password(_PWD),
                         db_path=self.db_path, init_database=init)

    def _key(self):
        self._k += 1
        return f"k{self._k}"

    def events(self, *, channel="chan_1", campaign=None, clicks=0, sales=0, amount=100.0,
               value_type=ValueType.REAL):
        for _ in range(clicks):
            self.app.facade.record_sales_event(self.owner, channel_id=channel, event_type="click",
                                               value_type=value_type, campaign_id=campaign, idempotency_key=self._key())
        for _ in range(sales):
            self.app.facade.record_sales_event(self.owner, channel_id=channel, event_type="sale",
                                               value_type=value_type, amount=amount, campaign_id=campaign,
                                               idempotency_key=self._key())

    def finance(self, entry_type, amount, channel="chan_1"):
        self.app.facade.record_finance_entry(self.owner, entry_type=entry_type, amount=amount, channel_id=channel,
                                             idempotency_key=self._key())

    def sql(self, query, params=()):
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute(query, params).fetchall()
        finally:
            conn.close()


# --------------------------------------------------------------- compilação
class CompilationTests(unittest.TestCase):
    def test_all_touched_modules_compile(self):
        import py_compile
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for rel in ("atlas/digital_twin.py", "atlas/learning.py", "atlas/director.py", "core/learning/loop.py",
                    "core/evidence/claims.py", "core/models/db.py"):
            py_compile.compile(os.path.join(root, rel), doraise=True)


# ------------------------------------------------------------- proveniência
class ProvenanceTests(_RealAppBase):
    def test_every_fact_points_to_real_rows_and_matches_them(self):
        self.events(campaign="camp_a", clicks=30, sales=3, amount=50.0)
        self.atlas.sync_digital_twin()
        rd = self.atlas.twin_state("campaign", "camp_a", "events.click")
        self.assertEqual(rd.status, ReadingStatus.CURRENT)
        fact = rd.facts[0]
        self.assertEqual(fact.source_id, "sales_tracking")
        self.assertIn("campaign_id=camp_a", fact.source_ref)
        n, last = self.sql("SELECT COUNT(*), MAX(created_at) FROM sales_events WHERE campaign_id='camp_a' "
                           "AND value_type='REAL' AND event_type IN ('click','sale')")[0]
        self.assertIn(f"n={n}", fact.source_ref)
        self.assertAlmostEqual(fact.observed_at, last, places=6)
        self.assertEqual(rd.fact_value, 30)
        self.assertGreater(fact.valid_until, fact.observed_at)
        self.assertTrue(fact.confirmed)
        self.assertTrue(0 < fact.confidence <= 1 and 0 <= fact.quality <= 1)

    def test_fact_without_provenance_is_rejected(self):
        twin = self.atlas.twin
        now = time.time()
        base = dict(entity_type="channel", entity_id="c", attribute="x", value=1, claim_type=ClaimType.FACT,
                    observed_at=now, valid_until=now + 60, confidence=0.9, quality=0.9, recorded_by="t")
        with self.assertRaises(TwinValidationError):
            twin.record(source_id="", source_ref="r", **base)
        with self.assertRaises(TwinValidationError):
            twin.record(source_id="s", source_ref="", **base)

    def test_unknown_is_never_invented(self):
        self.events(campaign="camp_a", clicks=5)
        self.atlas.sync_digital_twin()
        for attr in ("audience_size", "cac", "ltv", "competitor_price"):
            rd = self.atlas.twin_state("channel", "chan_1", attr)
            self.assertEqual(rd.status, ReadingStatus.UNKNOWN)
            self.assertIsNone(rd.fact_value)
        # sem vendas, receita e conversão continuam desconhecidas (não zero)
        self.assertEqual(self.atlas.twin_state("campaign", "camp_a", "revenue").status, ReadingStatus.UNKNOWN)
        self.assertEqual(self.atlas.twin_state("campaign", "camp_a", "conversion_rate").fact_value, 0.0)


# -------------------------------------------------- atualização e expiração
class UpdateAndExpiryTests(_RealAppBase):
    def test_new_real_data_creates_new_version_and_resync_is_idempotent(self):
        self.events(campaign="camp_a", clicks=10, sales=1)
        self.atlas.sync_digital_twin()
        v1 = self.atlas.twin_state("campaign", "camp_a", "events.click").facts[0]
        self.atlas.sync_digital_twin()
        self.assertEqual(len(self.atlas.twin.history("campaign", "camp_a", "events.click")), 1,
                         "re-sincronização sem dado novo não pode criar versão")
        self.events(campaign="camp_a", clicks=5)
        self.atlas.sync_digital_twin()
        v2 = self.atlas.twin_state("campaign", "camp_a", "events.click").facts[0]
        self.assertEqual((v2.version, v2.supersedes, v2.value), (v1.version + 1, v1.fact_id, 15))

    def test_fact_expires_and_stops_supporting_decisions(self):
        self.events(campaign="camp_a", clicks=10, sales=1)
        self.atlas.sync_digital_twin()
        later = time.time() + TTL_PERFORMANCE + 60
        rd = self.atlas.twin.reading("campaign", "camp_a", "conversion_rate", now=later)
        self.assertEqual(rd.status, ReadingStatus.EXPIRED)
        self.assertIsNone(rd.fact_value, "valor vencido nunca é apresentado como estado atual")
        self.assertEqual(len(rd.stale_facts), 1)
        gate = self.atlas.evidence_gate.assess(
            [EvidenceRequirement("campaign", "camp_a", "conversion_rate")], now=later)
        self.assertFalse(gate.allowed)
        self.assertIn("EXPIRED", gate.reasons()[0])

    def test_reobservation_renews_validity(self):
        self.events(campaign="camp_a", clicks=10, sales=1)
        self.atlas.sync_digital_twin()
        first = self.atlas.twin_state("campaign", "camp_a", "events.click").facts[0]
        self.events(campaign="camp_a", clicks=1)
        self.atlas.sync_digital_twin()
        second = self.atlas.twin_state("campaign", "camp_a", "events.click").facts[0]
        self.assertGreaterEqual(second.valid_until, first.valid_until)


# --------------------------------------------------- conflito entre fontes
class SourceConflictTests(_RealAppBase):
    def test_sales_vs_finance_revenue_conflict_is_explicit_and_blocks(self):
        self.events(clicks=10, sales=3, amount=150.0)      # vendas: 450
        self.finance("revenue", 300.0)                       # livro: 300
        self.atlas.sync_digital_twin()
        rd = self.atlas.twin_state("channel", "chan_1", "revenue")
        self.assertEqual(rd.status, ReadingStatus.CONFLICT)
        self.assertIsNone(rd.fact_value, "em conflito, nenhum valor é afirmado")
        self.assertEqual({f.source_id for f in rd.facts}, {"sales_tracking", "finance_ledger"})
        decision = self.atlas.decide_with_evidence(
            topic="budget", scope="channel:chan_1", chosen="aumentar orçamento",
            requirements=[EvidenceRequirement("channel", "chan_1", "revenue")], justification="x")
        self.assertEqual(decision.status, DecisionStatus.BLOCKED)
        self.assertIn("CONFLICT", " ".join(decision.evidence))

    def test_agreeing_sources_resolve_to_current(self):
        self.events(clicks=10, sales=2, amount=150.0)
        self.finance("revenue", 300.0)
        self.atlas.sync_digital_twin()
        rd = self.atlas.twin_state("channel", "chan_1", "revenue")
        self.assertEqual((rd.status, rd.fact_value), (ReadingStatus.CURRENT, 300.0))
        self.assertEqual(len(rd.facts), 2, "concordância confirmada por duas fontes independentes")

    def test_conflict_resolves_when_ledger_is_reconciled(self):
        self.events(clicks=10, sales=3, amount=150.0)
        self.finance("revenue", 300.0)
        self.atlas.sync_digital_twin()
        self.finance("revenue", 150.0)   # lançamento faltante no livro
        self.atlas.sync_digital_twin()
        rd = self.atlas.twin_state("channel", "chan_1", "revenue")
        self.assertEqual((rd.status, rd.fact_value), (ReadingStatus.CURRENT, 450.0))


# ------------------------------------------------------------------ histórico
class HistoryTests(_RealAppBase):
    def test_history_is_append_only_and_survives_restart(self):
        for n in (4, 3, 2):
            self.events(campaign="camp_a", clicks=n)
            self.atlas.sync_digital_twin()
        hist = self.atlas.twin.history("campaign", "camp_a", "events.click")
        self.assertEqual([f.value for f in hist], [4, 7, 9])
        self.assertEqual([f.version for f in hist], [1, 2, 3])
        self.assertEqual(hist[2].supersedes, hist[1].fact_id)
        app2 = self._build(init=False)
        hist2 = app2.atlas.twin.history("campaign", "camp_a", "events.click")
        self.assertEqual([(f.fact_id, f.value) for f in hist2], [(f.fact_id, f.value) for f in hist])
        rows = self.sql("SELECT COUNT(*) FROM atlas_twin_facts WHERE entity_id='camp_a' AND attribute='events.click'")
        self.assertEqual(rows[0][0], 3)


# ------------------------------------ separação fato/inferência/hipótese/previsão
class EpistemicSeparationTests(_RealAppBase):
    def test_forecast_and_estimate_never_become_facts(self):
        self.events(clicks=10, sales=1, amount=100.0)                                   # REAL
        self.events(sales=5, amount=100.0, value_type=ValueType.FORECAST)             # previsão
        self.events(sales=2, amount=100.0, value_type=ValueType.ESTIMATED)            # estimativa
        self.atlas.sync_digital_twin()
        rd = self.atlas.twin_state("channel", "chan_1", "revenue")
        self.assertEqual((rd.status, rd.fact_value), (ReadingStatus.CURRENT, 100.0))
        classes = {(f.claim_type, f.value) for f in rd.non_factual}
        self.assertIn((ClaimType.FORECAST, 500.0), classes)
        self.assertIn((ClaimType.INFERENCE, 200.0), classes)
        self.assertTrue(all(f.claim_type == ClaimType.FACT for f in rd.facts))

    def test_only_non_factual_means_not_observed(self):
        self.events(channel="chan_9", sales=4, amount=10.0, value_type=ValueType.FORECAST)
        self.atlas.sync_digital_twin()
        rd = self.atlas.twin_state("channel", "chan_9", "revenue")
        self.assertEqual(rd.status, ReadingStatus.NOT_OBSERVED)
        self.assertIsNone(rd.fact_value)
        self.assertEqual(rd.non_factual[0].claim_type, ClaimType.FORECAST)

    def test_class_rules_are_structural(self):
        twin, now = self.atlas.twin, time.time()
        common = dict(entity_type="channel", entity_id="c", attribute="a", value=1.0, source_id="s",
                      source_ref="r", observed_at=now, valid_until=now + 60, confidence=0.5, quality=0.5,
                      recorded_by="t")
        for bad in (ClaimType.ESTIMATE, ClaimType.UNKNOWN):
            with self.assertRaises(TwinValidationError):
                twin.record(claim_type=bad, basis="b", **common)
        for needs_basis in (ClaimType.INFERENCE, ClaimType.FORECAST, ClaimType.HYPOTHESIS):
            with self.assertRaises(TwinValidationError):
                twin.record(claim_type=needs_basis, **common)
        with self.assertRaises(TwinValidationError):
            twin.record(claim_type=ClaimType.INFERENCE, basis="b", derived_from=["tf_inexistente"], **common)
        with self.assertRaises(TwinValidationError):
            twin.record(claim_type=ClaimType.FACT, **{**common, "observed_at": now + 3600, "valid_until": now + 7200})
        with self.assertRaises(TwinValidationError):
            twin.record(claim_type=ClaimType.FACT, **{**common, "value": None})

    def test_classes_persist_unchanged_across_restart(self):
        self.events(clicks=10, sales=1)
        self.events(sales=3, value_type=ValueType.FORECAST)
        self.atlas.sync_digital_twin()
        app2 = self._build(init=False)
        rd = app2.atlas.twin_state("channel", "chan_1", "revenue")
        self.assertEqual(rd.facts[0].claim_type, ClaimType.FACT)
        self.assertEqual(rd.non_factual[0].claim_type, ClaimType.FORECAST)
        self.assertEqual(rd.fact_value, 100.0)


# ------------------------------------ decisão bloqueada por evidência insuficiente
class EvidenceBlockedDecisionTests(_RealAppBase):
    def test_missing_evidence_blocks_and_is_remembered(self):
        self.events(clicks=10, sales=1)
        self.finance("revenue", 100.0)
        self.atlas.sync_digital_twin()
        d = self.atlas.decide_with_evidence(
            topic="scale", scope="channel:chan_1", chosen="dobrar orçamento", justification="margem boa",
            requirements=[EvidenceRequirement("channel", "chan_1", "margin"),
                          EvidenceRequirement("channel", "chan_1", "revenue")])
        self.assertEqual(d.status, DecisionStatus.BLOCKED)
        self.assertIsNone(d.chosen)
        self.assertTrue(any("margin = UNKNOWN" in e for e in d.evidence))
        self.finance("expense", 40.0)
        self.atlas.sync_digital_twin()
        d2 = self.atlas.decide_with_evidence(
            topic="scale", scope="channel:chan_1", chosen="dobrar orçamento", justification="margem factual 60",
            requirements=[EvidenceRequirement("channel", "chan_1", "margin"),
                          EvidenceRequirement("channel", "chan_1", "revenue")])
        self.assertEqual((d2.status, d2.chosen, d2.supersedes), (DecisionStatus.DECIDED, "dobrar orçamento",
                                                                 d.decision_id))
        self.assertEqual(self.atlas.twin_state("channel", "chan_1", "margin").fact_value, 60.0)
        margin_fact = self.atlas.twin_state("channel", "chan_1", "margin").facts[0].fact_id
        self.assertIn(margin_fact, d2.evidence)

    def test_low_confidence_fact_does_not_support_decision(self):
        now = time.time()
        self.atlas.twin.record(entity_type="competition", entity_id="op1", attribute="signal.demand", value=0.9,
                               claim_type=ClaimType.FACT, source_id="discovery:web", source_ref="acq_1",
                               observed_at=now, valid_until=now + 3600, confidence=0.3, quality=0.5,
                               confirmed=True, recorded_by="t")
        rd = self.atlas.twin_state("competition", "op1", "signal.demand")
        self.assertEqual(rd.status, ReadingStatus.LOW_CONFIDENCE)
        self.assertFalse(self.atlas.evidence_gate.assess(
            [EvidenceRequirement("competition", "op1", "signal.demand")]).allowed)

    def test_no_evidence_means_abstain_not_act(self):
        rec = self.atlas.adaptive_cycle.recommend(target_type="campaign", target_id="nunca_vista",
                                                  metric_name="conversion_rate")
        self.assertEqual(rec["action"], Recommendation.ABSTAIN)
        self.assertEqual(self.atlas.decisions.get(rec["decision_id"]).status, DecisionStatus.ABSTAINED)

    def test_hypothesis_without_baseline_is_blocked(self):
        with self.assertRaises(InsufficientEvidence):
            self.atlas.adaptive_cycle.define_hypothesis(
                target_type="campaign", target_id="sem_dados", metric_name="conversion_rate",
                hypothesis="h", expected_value=0.1)
        last = self.atlas.decisions.latest(topic="hypothesis:conversion_rate", scope="campaign:sem_dados")
        self.assertEqual(last.status, DecisionStatus.BLOCKED)


# ---------------------------------------------- ciclo adaptativo (resultado real)
class AdaptiveCycleTests(_RealAppBase):
    def _round(self, strategy_id, treat=None, control=None):
        self.atlas.adaptive_cycle.start_round(strategy_id) if not self._first else None
        self._first = False
        if treat:
            self.events(campaign=treat[0], clicks=treat[1], sales=treat[2])
        if control:
            self.events(campaign=control[0], clicks=control[1], sales=control[2])
        self.atlas.sync_digital_twin()
        return self.atlas.adaptive_cycle.measure_round(strategy_id)

    def test_controlled_success_is_learned_recalibrated_and_scaled(self):
        self.events(campaign="camp_t", clicks=100, sales=2)
        self.events(campaign="camp_c", clicks=100, sales=2)
        self.atlas.sync_digital_twin()
        s = self.atlas.adaptive_cycle.define_hypothesis(
            target_type="campaign", target_id="camp_t", metric_name="conversion_rate",
            hypothesis="novo gancho eleva a conversão", expected_value=0.05, design=TestDesign.CONTROLLED,
            control_id="camp_c")
        self._first = True
        errors = []
        for _ in range(5):
            r = self._round(s.strategy_id, treat=("camp_t", 100, 5), control=("camp_c", 100, 2))
            errors.append(abs(r.error))
        s = self.atlas.adaptive_cycle.get(s.strategy_id)
        self.assertEqual((s.status, s.causal_status), (StrategyStatus.VALIDATED, CausalStatus.CAUSAL_SUPPORTED))
        self.assertLess(errors[-1], errors[0], "recalibração deve reduzir o erro de previsão")
        learned = self.atlas.quantitative_learning_loop.learnings_visible_to(
            scope_channel_id="campaign:camp_t", scope_project_id=s.strategy_id)
        self.assertEqual(len(learned), 1)
        self.assertIn("controle", learned[0].statement)
        rec = self.atlas.adaptive_cycle.recommend(target_type="campaign", target_id="camp_t",
                                                  metric_name="conversion_rate")
        self.assertEqual((rec["action"], rec["strategy_id"]), (Recommendation.SCALE, s.strategy_id))

    def test_refuted_strategy_is_abandoned_and_not_rescued(self):
        self.events(campaign="camp_a", clicks=100, sales=4)
        self.atlas.sync_digital_twin()
        s = self.atlas.adaptive_cycle.define_hypothesis(
            target_type="campaign", target_id="camp_a", metric_name="conversion_rate",
            hypothesis="thumbnail escura eleva a conversão", expected_value=0.06)
        self._first = True
        for _ in range(5):
            self._round(s.strategy_id, treat=("camp_a", 100, 1))
        s = self.atlas.adaptive_cycle.get(s.strategy_id)
        self.assertEqual((s.status, s.causal_status), (StrategyStatus.ABANDONED, CausalStatus.REFUTED))
        with self.assertRaises(ValueError):
            self.atlas.adaptive_cycle.start_round(s.strategy_id)
        dec = self.atlas.decisions.latest(topic=f"strategy:{s.strategy_id}", scope="campaign:camp_a")
        self.assertEqual(dec.chosen, "ABANDON")
        self.assertEqual(self.atlas.adaptive_cycle.recommend(
            target_type="campaign", target_id="camp_a", metric_name="conversion_rate")["action"],
            Recommendation.ABANDON)

    def test_common_trend_is_correlation_not_causation(self):
        """Mesmos dados reais: sem controle a hipótese 'valida' como
        correlação; com controle (que subiu igual) é refutada."""
        for camp in ("camp_obs", "camp_t", "camp_c"):
            self.events(campaign=camp, clicks=100, sales=2)
        self.atlas.sync_digital_twin()
        cycle = self.atlas.adaptive_cycle
        obs = cycle.define_hypothesis(target_type="campaign", target_id="camp_obs", metric_name="conversion_rate",
                                      hypothesis="gancho eleva conversão", expected_value=0.04)
        ctl = cycle.define_hypothesis(target_type="campaign", target_id="camp_t", metric_name="conversion_rate",
                                      hypothesis="gancho eleva conversão", expected_value=0.04,
                                      design=TestDesign.CONTROLLED, control_id="camp_c")
        for i in range(5):
            if i:
                cycle.start_round(obs.strategy_id)
                cycle.start_round(ctl.strategy_id)
            for camp in ("camp_obs", "camp_t", "camp_c"):      # tendência comum: todos sobem igual
                self.events(campaign=camp, clicks=100, sales=5)
            self.atlas.sync_digital_twin()
            cycle.measure_round(obs.strategy_id)
            cycle.measure_round(ctl.strategy_id)
        self.assertEqual(cycle.get(obs.strategy_id).causal_status, CausalStatus.CORRELATIONAL_ONLY)
        self.assertEqual(cycle.get(ctl.strategy_id).causal_status, CausalStatus.REFUTED)
        rec = cycle.recommend(target_type="campaign", target_id="camp_obs", metric_name="conversion_rate")
        self.assertEqual(rec["action"], Recommendation.TEST_WITH_CONTROL)

    def test_measurement_requires_new_real_observation(self):
        self.events(campaign="camp_a", clicks=100, sales=2)
        self.atlas.sync_digital_twin()
        s = self.atlas.adaptive_cycle.define_hypothesis(
            target_type="campaign", target_id="camp_a", metric_name="conversion_rate", hypothesis="h",
            expected_value=0.05)
        with self.assertRaises(InsufficientEvidence):
            self.atlas.adaptive_cycle.measure_round(s.strategy_id)

    def test_cycle_state_survives_restart(self):
        self.events(campaign="camp_a", clicks=100, sales=2)
        self.atlas.sync_digital_twin()
        s = self.atlas.adaptive_cycle.define_hypothesis(
            target_type="campaign", target_id="camp_a", metric_name="conversion_rate", hypothesis="h",
            expected_value=0.05)
        self.events(campaign="camp_a", clicks=100, sales=6)
        self.atlas.sync_digital_twin()
        self.atlas.adaptive_cycle.measure_round(s.strategy_id)
        app2 = self._build(init=False)
        s2 = app2.atlas.adaptive_cycle.get(s.strategy_id)
        self.assertIsNotNone(s2)
        self.assertEqual(len(s2.rounds), 1)
        self.assertTrue(s2.rounds[0]["measured"])
        records = [r for r in app2.atlas.quantitative_learning_loop.all_records()
                   if r.scope_project_id == s.strategy_id]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].baseline_value, 0.02)
        app2.atlas.adaptive_cycle.start_round(s.strategy_id)   # continua de onde parou


# ------------------------ previsão × realidade (defeito provado no loop quantitativo)
class PredictionVsRealityTests(unittest.TestCase):
    def _loop(self, predicted, actuals, baseline=None):
        loop = LearningLoop()
        for a in actuals:
            p = loop.record_prediction(Prediction.new(agent_id="A", metric_name="conversion_rate",
                                                      predicted_value=predicted, context="c",
                                                      scope_channel_id="c1", baseline_value=baseline))
            loop.record_outcome(prediction_id=p.prediction_id, actual_value=a)
        return loop.evaluate_learning(metric_name="conversion_rate", scope_channel_id="c1")

    def test_refuted_level_prediction_is_never_validated(self):
        # antes: previsto 10% × real ~1% (erro ~900%) era VALIDATED
        self.assertEqual(self._loop(0.10, [0.010, 0.012, 0.009, 0.011, 0.008]), ValidationStatus.CONTRADICTED)

    def test_baseline_direction(self):
        self.assertEqual(self._loop(0.015, [0.014, 0.016, 0.013, 0.015, 0.017], baseline=0.011),
                         ValidationStatus.VALIDATED)
        self.assertEqual(self._loop(0.015, [0.010, 0.012, 0.009, 0.011, 0.008], baseline=0.011),
                         ValidationStatus.CONTRADICTED)

    def test_baseline_persists_and_old_databases_migrate(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db_module.remove_db_file(path)
        try:
            conn = sqlite3.connect(path)
            try:
                conn.execute("CREATE TABLE learning_predictions (prediction_id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, "
                             "metric_name TEXT NOT NULL, predicted_value REAL NOT NULL, scope_channel_id TEXT, "
                             "scope_project_id TEXT, context TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, "
                             "status TEXT NOT NULL DEFAULT 'PENDING', evidence_ids TEXT NOT NULL DEFAULT '[]')")
                conn.execute("INSERT INTO learning_predictions (prediction_id, agent_id, metric_name, predicted_value, "
                             "created_at) VALUES ('old', 'A', 'm', 1.0, 1.0)")
                conn.commit()
            finally:
                conn.close()
            db_module.init_db(path)
            loop = LearningLoop(db_path=path)
            self.assertIsNone(loop._predictions["old"].baseline_value)
            p = loop.record_prediction(Prediction.new(agent_id="A", metric_name="m", predicted_value=2.0,
                                                      context="c", baseline_value=1.5))
            loop.record_outcome(prediction_id=p.prediction_id, actual_value=1.8)
            loop2 = LearningLoop(db_path=path)
            self.assertEqual(loop2._predictions[p.prediction_id].baseline_value, 1.5)
            self.assertEqual([r.baseline_value for r in loop2.all_records()], [1.5])
        finally:
            db_module.remove_db_file(path)


# ------------------------------------------------ conversacionais do ATLAS
class AtlasConversationTests(_RealAppBase):
    def _prepare(self):
        self.events(channel="chan_1", campaign="camp_a", clicks=100, sales=3, amount=100.0)
        self.finance("revenue", 300.0, channel="chan_1")
        self.finance("expense", 120.0, channel="chan_1")
        self.events(channel="chan_2", clicks=50, sales=1, amount=80.0)
        self.atlas.sync_digital_twin()

    def test_long_dialogue_continuity_context_switch_and_recall(self):
        self._prepare()
        c = self.atlas.consultation()
        c.switch_context("channel", "chan_1")
        first = self.atlas.decide_with_evidence(
            topic="budget", scope="channel:chan_1", chosen="manter orçamento", justification="margem factual 180",
            requirements=[EvidenceRequirement("channel", "chan_1", "margin")])
        for i in range(300):                                  # diálogo longo com trocas de contexto
            if i % 3 == 0:
                c.switch_context("channel", "chan_2")
                self.assertEqual(c.ask_state("revenue")["fact_value"], 80.0)
                self.assertEqual(c.recall("budget")["status"], "UNKNOWN", "decisão de chan_1 não vaza para chan_2")
            elif i % 3 == 1:
                c.switch_context("campaign", "camp_a")
                self.assertEqual(c.ask_state("conversion_rate")["fact_value"], 0.03)
            else:
                c.switch_context("channel", "chan_1")
                ans = c.recall("budget")
                self.assertEqual((ans["decision_id"], ans["chosen"]), (first.decision_id, "manter orçamento"))
        self.assertGreater(len(c.turns), 600)
        why = c.why(first.decision_id)
        margin_fact = self.atlas.twin_state("channel", "chan_1", "margin").facts[0]
        self.assertIn(margin_fact.fact_id, why["evidence"])

    def test_decisions_are_recovered_in_a_new_session_after_restart(self):
        self._prepare()
        d = self.atlas.decide_with_evidence(
            topic="budget", scope="channel:chan_1", chosen="manter orçamento", justification="margem factual",
            requirements=[EvidenceRequirement("channel", "chan_1", "margin")])
        app2 = self._build(init=False)
        c2 = app2.atlas.consultation()
        c2.switch_context("channel", "chan_1")
        ans = c2.recall("budget")
        self.assertEqual((ans["decision_id"], ans["chosen"], ans["status"]), (d.decision_id, "manter orçamento",
                                                                              "DECIDED"))

    def test_answers_label_every_class_and_state(self):
        self._prepare()
        self.events(channel="chan_1", sales=4, amount=100.0, value_type=ValueType.FORECAST)
        self.atlas.sync_digital_twin()
        c = self.atlas.consultation()
        c.switch_context("channel", "chan_1")
        rev = c.ask_state("revenue")
        self.assertEqual(rev["status"], "CURRENT")
        self.assertTrue(rev["statement"].startswith("FATO OBSERVADO"))
        self.assertEqual([n["class"] for n in rev["non_factual"]], ["FORECAST"])
        unknown = c.ask_state("audience_size")
        self.assertTrue(unknown["statement"].startswith("DESCONHECIDO"))
        self.assertIsNone(unknown["fact_value"])

    def test_comparison_uses_only_facts_and_flags_missing(self):
        self._prepare()
        d = self.atlas.compare_alternatives(topic="best_channel", scope="global", metric="revenue",
                                            alternatives=[("channel", "chan_1"), ("channel", "chan_2"),
                                                          ("channel", "chan_x")])
        self.assertEqual(d.chosen, "channel:chan_1")
        self.assertIn("channel:chan_x=UNKNOWN", d.alternatives)
        self.assertIn("channel:chan_x=UNKNOWN", d.uncertainty, "alternativa sem evidência declarada na incerteza")
        self.assertEqual(d.status, DecisionStatus.DECIDED)
        none = self.atlas.compare_alternatives(topic="best_x", scope="global", metric="revenue",
                                               alternatives=[("channel", "a"), ("channel", "b")])
        self.assertEqual(none.status, DecisionStatus.ABSTAINED)

    def test_grounded_disagreement_with_owner_proposal(self):
        self.events(campaign="camp_a", clicks=100, sales=4)
        self.atlas.sync_digital_twin()
        cycle = self.atlas.adaptive_cycle
        s = cycle.define_hypothesis(target_type="campaign", target_id="camp_a", metric_name="conversion_rate",
                                    hypothesis="thumbnail escura eleva conversão", expected_value=0.06)
        c = self.atlas.consultation()
        early = c.propose(s.strategy_id, "SCALE")
        self.assertEqual(early["status"], "ABSTAINED", "sem resultado real ainda: não concorda nem discorda")
        for i in range(5):
            if i:
                cycle.start_round(s.strategy_id)
            self.events(campaign="camp_a", clicks=100, sales=1)
            self.atlas.sync_digital_twin()
            cycle.measure_round(s.strategy_id)
        late = c.propose(s.strategy_id, "SCALE")
        self.assertEqual(late["status"], "DISAGREED")
        self.assertIn("REFUTOU", late["statement"])


if __name__ == "__main__":
    unittest.main()
