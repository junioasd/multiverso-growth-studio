"""
atlas.learning
================
ATLAS — item 17: aprender com resultados reais através do Learning
Loop sem modificar silenciosamente regras protegidas.

`AtlasLearningCoordinator` só sabe fazer UMA coisa: registrar uma
observação (`worked` | `failed` | `inconclusive`) no
`commercial.learning_loop.LearningLoop` já existente, e consultar os
padrões que ele já calcula. Não existe nenhum método aqui — nem em
`LearningLoop` — capaz de alterar RBAC, Quality Gates, isolamento,
schema ou qualquer outra regra protegida do sistema. "Aprender" para o
ATLAS significa exclusivamente acumular observações rotuladas para
consulta futura, nunca reescrever código ou configuração.
"""

from __future__ import annotations

from commercial.learning_loop import LearningLoop, LearningPattern, LearningRecord
from core.security.audit import AuditTrail


class AtlasLearningCoordinator:
    def __init__(self, learning_loop: LearningLoop, audit: AuditTrail) -> None:
        self.learning_loop = learning_loop
        self.audit = audit

    def record_observation(
        self, *, scope_key: str, outcome: str, context: dict, limitations: str = "", actor: str
    ) -> LearningRecord:
        record = self.learning_loop.record(
            scope_key=scope_key, outcome=outcome, context=context, limitations=limitations
        )
        self.audit.record(
            actor=actor, action="atlas_learning_observation", resource=scope_key,
            outcome="SUCCESS", details={"outcome": outcome},
        )
        return record

    def pattern_for(self, scope_key: str) -> LearningPattern:
        return self.learning_loop.pattern_for(scope_key)


# ======================================================================
# COMANDO 2/6 — CICLO ADAPTATIVO COMPROVÁVEL
#
# observar → interpretar → formular estratégia → definir hipótese →
# executar/testar → medir resultado real → comparar previsão × realidade
# → aprender → recalibrar → adaptar
#
# Reutiliza (não duplica): `atlas.digital_twin` (observação factual e
# registro de hipótese/previsão), `core.learning.loop.LearningLoop`
# (previsão × realidade persistente, com a correção de direção por linha
# de base) e `atlas.digital_twin.DecisionLedger` (memória de decisões).
# ======================================================================
import json as _json
import time as _time
import uuid as _uuid
from dataclasses import dataclass as _dataclass
from enum import Enum as _Enum

from atlas.digital_twin import (
    DecisionLedger, DecisionStatus, DigitalTwin, EvidenceGate, EvidenceRequirement,
)
from core.evidence.claims import ClaimType
from core.learning.loop import LearningLoop as QuantitativeLearningLoop
from core.learning.loop import Prediction, ValidationStatus
from core.models import db as _db_module


class StrategyStatus(str, _Enum):
    TESTING = "TESTING"
    VALIDATED = "VALIDATED"
    ABANDONED = "ABANDONED"
    # COMANDO 4/6: validada num regime de mercado que MUDOU de forma
    # persistente — a validação antiga não governa mais a decisão atual.
    NEEDS_REVALIDATION = "NEEDS_REVALIDATION"


class TestDesign(str, _Enum):
    CONTROLLED = "CONTROLLED"          # tratamento × controle (diferença-em-diferenças)
    OBSERVATIONAL = "OBSERVATIONAL"    # sem controle — no máximo correlação


class CausalStatus(str, _Enum):
    UNTESTED = "UNTESTED"
    CORRELATIONAL_ONLY = "CORRELATIONAL_ONLY"   # validado sem controle: NÃO é prova de causa
    CAUSAL_SUPPORTED = "CAUSAL_SUPPORTED"       # validado com grupo de controle
    REFUTED = "REFUTED"


class Recommendation(str, _Enum):
    SCALE = "SCALE"
    HOLD = "HOLD"                    # possível mudança ainda NÃO confirmada: não escalar às cegas
    REVALIDATE = "REVALIDATE"        # regime mudou: testar de novo antes de agir
    TEST_WITH_CONTROL = "TEST_WITH_CONTROL"
    CONTINUE_TEST = "CONTINUE_TEST"
    ABANDON = "ABANDON"
    ABSTAIN = "ABSTAIN"


class InsufficientEvidence(Exception):
    def __init__(self, message: str, reasons: list | None = None) -> None:
        super().__init__(message)
        self.reasons = list(reasons or [])


@_dataclass
class Strategy:
    strategy_id: str
    target_type: str
    target_id: str
    metric_name: str
    hypothesis: str
    design: TestDesign
    control_id: str | None
    status: StrategyStatus
    causal_status: CausalStatus
    hypothesis_fact_id: str
    rounds: list              # [{prediction_id, baseline, control_baseline, started_at, expected, forecast_fact_id}]
    last_evaluation: str
    created_at: float
    updated_at: float

    @property
    def scope_key(self) -> str:
        return f"{self.target_type}:{self.target_id}"


class AdaptiveStrategyCycle:
    """Executa o ciclo adaptativo sobre estado REAL do Gêmeo. Cada
    estratégia é isolada no loop quantitativo por `scope_project_id =
    strategy_id` — resultados de uma estratégia nunca contaminam outra."""

    AGENT_ID = "ATLAS"

    def __init__(self, *, twin: DigitalTwin, learning_loop: QuantitativeLearningLoop, ledger: DecisionLedger,
                 gate: EvidenceGate, actor: str, db_path: str | None = None) -> None:
        self.twin, self.loop, self.ledger, self.gate = twin, learning_loop, ledger, gate
        self.actor, self.db_path = actor, db_path
        self._strategies: dict = {}
        if db_path:
            self._load()

    # -------------------------------------------------- observar/interpretar
    def _require_current(self, entity_type: str, entity_id: str, attribute: str, purpose: str):
        assessment = self.gate.assess([EvidenceRequirement(entity_type, entity_id, attribute, purpose)])
        if not assessment.allowed:
            raise InsufficientEvidence(f"evidência insuficiente para {purpose}", assessment.reasons())
        return assessment.readings[0]

    # ------------------------------------ formular estratégia / hipótese
    def define_hypothesis(self, *, target_type: str, target_id: str, metric_name: str, hypothesis: str,
                          expected_value: float, design: TestDesign = TestDesign.OBSERVATIONAL,
                          control_id: str | None = None, now: float | None = None) -> Strategy:
        now = _time.time() if now is None else now
        design = TestDesign(design)
        if design == TestDesign.CONTROLLED and not control_id:
            raise ValueError("desenho CONTROLLED exige control_id (grupo de controle)")
        scope = f"{target_type}:{target_id}"
        try:
            baseline = self._require_current(target_type, target_id, metric_name, "linha de base da hipótese")
            control = None
            if design == TestDesign.CONTROLLED:
                control = self._require_current(target_type, control_id, metric_name, "linha de base do controle")
        except InsufficientEvidence as exc:
            self.ledger.record(
                topic=f"hypothesis:{metric_name}", scope=scope, status=DecisionStatus.BLOCKED, actor=self.actor,
                justification="hipótese não iniciada: sem linha de base factual vigente", evidence=exc.reasons,
                uncertainty="linha de base desconhecida/vencida/conflitante", now=now,
            )
            raise
        strategy_id = f"strat_{_uuid.uuid4().hex[:12]}"
        base_fact = baseline.facts[0]
        hyp = self.twin.record(
            entity_type="strategy", entity_id=strategy_id, attribute="hypothesis",
            value={"statement": hypothesis, "target": scope, "metric": metric_name, "design": design.value,
                   "control": control_id},
            claim_type=ClaimType.HYPOTHESIS, source_id="atlas.adaptive_cycle", source_ref=strategy_id,
            basis=hypothesis, observed_at=now, valid_until=now + 365 * 24 * 3600.0, confidence=0.5,
            quality=1.0, derived_from=[f.fact_id for f in baseline.facts], recorded_by=self.actor, now=now,
        )
        strategy = Strategy(
            strategy_id=strategy_id, target_type=target_type, target_id=target_id, metric_name=metric_name,
            hypothesis=hypothesis, design=design, control_id=control_id, status=StrategyStatus.TESTING,
            causal_status=CausalStatus.UNTESTED, hypothesis_fact_id=hyp.fact_id, rounds=[],
            last_evaluation="", created_at=now, updated_at=now,
        )
        self._strategies[strategy_id] = strategy
        self.ledger.record(
            topic=f"hypothesis:{metric_name}", scope=scope, status=DecisionStatus.DECIDED, actor=self.actor,
            chosen=strategy_id, justification=f"testar hipótese: {hypothesis}",
            evidence=[base_fact.fact_id] + ([control.facts[0].fact_id] if control else []),
            uncertainty="hipótese não verificada; causalidade exige controle" if design != TestDesign.CONTROLLED
            else "hipótese não verificada", now=now,
        )
        self.start_round(strategy_id, expected_value=expected_value, now=now)
        return strategy

    # ------------------------------------------------------ executar/testar
    def start_round(self, strategy_id: str, *, expected_value: float | None = None,
                    now: float | None = None) -> str:
        now = _time.time() if now is None else now
        s = self._get(strategy_id)
        if s.status == StrategyStatus.ABANDONED:
            raise ValueError("estratégia ABANDONADA não recebe novas rodadas")
        if any(r["measured"] is False for r in s.rounds):
            raise ValueError("rodada anterior ainda sem resultado real medido")
        baseline = self._require_current(s.target_type, s.target_id, s.metric_name, "linha de base da rodada")
        control_baseline = None
        if s.design == TestDesign.CONTROLLED:
            control_baseline = self._require_current(
                s.target_type, s.control_id, s.metric_name, "linha de base do controle").fact_value
        if expected_value is None:
            expected_value = self.recalibrated_expectation(strategy_id, baseline=baseline.fact_value)
        forecast = self.twin.record(
            entity_type="strategy", entity_id=strategy_id, attribute=f"forecast.{s.metric_name}",
            value=expected_value, claim_type=ClaimType.FORECAST, source_id="atlas.adaptive_cycle",
            source_ref=f"{strategy_id}#round{len(s.rounds) + 1}",
            basis="expectativa recalibrada pelo efeito real médio" if s.rounds else "expectativa inicial da hipótese",
            observed_at=now, valid_until=now + 365 * 24 * 3600.0, confidence=0.5, quality=1.0,
            derived_from=[f.fact_id for f in baseline.facts] + [s.hypothesis_fact_id], recorded_by=self.actor,
            now=now,
        )
        prediction = self.loop.record_prediction(Prediction.new(
            agent_id=self.AGENT_ID, metric_name=s.metric_name, predicted_value=float(expected_value),
            baseline_value=float(baseline.fact_value), context=s.hypothesis, scope_channel_id=s.scope_key,
            scope_project_id=strategy_id, evidence_ids=[f.fact_id for f in baseline.facts],
        ))
        s.rounds.append({"prediction_id": prediction.prediction_id, "baseline": float(baseline.fact_value),
                         "control_baseline": control_baseline, "started_at": now, "expected": float(expected_value),
                         "forecast_fact_id": forecast.fact_id, "measured": False, "actual": None})
        s.updated_at = now
        self._persist(s)
        return prediction.prediction_id

    # ------------------------------ medir resultado real / comparar / aprender
    def measure_round(self, strategy_id: str, *, now: float | None = None):
        now = _time.time() if now is None else now
        s = self._get(strategy_id)
        open_rounds = [r for r in s.rounds if r["measured"] is False]
        if not open_rounds:
            raise ValueError("nenhuma rodada aberta para medir")
        rnd = open_rounds[0]
        reading = self._require_current(s.target_type, s.target_id, s.metric_name, "resultado real da rodada")
        if max(f.observed_at for f in reading.facts) <= rnd["started_at"]:
            raise InsufficientEvidence(
                "nenhuma observação real POSTERIOR ao início da rodada — medir agora seria repetir a linha de base",
                [f"último observed_at <= início da rodada ({rnd['started_at']})"],
            )
        actual = float(reading.fact_value)
        if s.design == TestDesign.CONTROLLED:
            control = self._require_current(s.target_type, s.control_id, s.metric_name, "resultado do controle")
            if max(f.observed_at for f in control.facts) <= rnd["started_at"]:
                raise InsufficientEvidence("controle sem observação posterior ao início da rodada")
            # diferença-em-diferenças: remove do tratamento o movimento
            # que o controle também teve (tendência comum não é efeito).
            actual = actual - (float(control.fact_value) - rnd["control_baseline"])
        record = self.loop.record_outcome(prediction_id=rnd["prediction_id"], actual_value=actual)
        rnd["measured"], rnd["actual"] = True, actual
        rnd["evidence"] = [f.fact_id for f in reading.facts]
        s.updated_at = now
        self.evaluate_and_recalibrate(strategy_id, now=now)
        return record

    # -------------------------------------------- recalibrar / adaptar
    def evaluate_and_recalibrate(self, strategy_id: str, *, now: float | None = None) -> ValidationStatus:
        now = _time.time() if now is None else now
        s = self._get(strategy_id)
        status = self.loop.evaluate_learning(
            metric_name=s.metric_name, scope_channel_id=s.scope_key, scope_project_id=strategy_id)
        records = self._records(strategy_id)
        summary = {
            "validation": status.value, "rounds_measured": len(records),
            "direction_accuracy": (sum(1 for r in records if r.direction_correct) / len(records)) if records else None,
            "mean_real_effect": self._mean_effect(records),
        }
        s.last_evaluation = _json.dumps(summary)
        if status == ValidationStatus.CONTRADICTED and s.status != StrategyStatus.ABANDONED:
            s.status, s.causal_status = StrategyStatus.ABANDONED, CausalStatus.REFUTED
            self.ledger.record(
                topic=f"strategy:{strategy_id}", scope=s.scope_key, status=DecisionStatus.DECIDED,
                chosen="ABANDON", actor=self.actor, now=now,
                justification=f"realidade contradisse a hipótese em {len(records)} rodadas medidas "
                              f"(acerto de direção {summary['direction_accuracy']:.0%})",
                evidence=[e for r in s.rounds for e in r.get("evidence", [])], uncertainty="",
            )
        elif status == ValidationStatus.VALIDATED and s.status == StrategyStatus.TESTING:
            s.status = StrategyStatus.VALIDATED
            s.causal_status = (CausalStatus.CAUSAL_SUPPORTED if s.design == TestDesign.CONTROLLED
                               else CausalStatus.CORRELATIONAL_ONLY)
            label = "efeito causal sustentado por controle" if s.design == TestDesign.CONTROLLED \
                else "CORRELAÇÃO observada — sem controle, causalidade não demonstrada"
            self.loop.promote_validated_learning(
                metric_name=s.metric_name, statement=f"{s.hypothesis} [{label}]", validated_by=self.actor,
                scope_channel_id=s.scope_key, scope_project_id=strategy_id,
            )
            self.ledger.record(
                topic=f"strategy:{strategy_id}", scope=s.scope_key, status=DecisionStatus.DECIDED,
                chosen="VALIDATED", actor=self.actor, now=now, justification=f"{len(records)} rodadas reais: {label}",
                evidence=[e for r in s.rounds for e in r.get("evidence", [])],
                uncertainty="" if s.design == TestDesign.CONTROLLED else "causalidade não demonstrada",
            )
        s.updated_at = now
        self._persist(s)
        return status

    def recalibrated_expectation(self, strategy_id: str, *, baseline: float) -> float:
        """Expectativa da próxima rodada. A recalibração ajusta a
        MAGNITUDE do efeito pelo efeito REAL médio medido — mas NUNCA
        inverte o SENTIDO da hipótese: se a realidade foi contra a
        hipótese, a hipótese continua sendo testada como formulada (e a
        realidade continuará contradizendo-a até o abandono). Inverter o
        sentido faria rodadas seguintes "acertarem" e resgatariam uma
        hipótese refutada."""
        s = self._get(strategy_id)
        if not s.rounds:
            return float(baseline)
        first = s.rounds[0]
        hypothesis_delta = first["expected"] - first["baseline"]
        effect = self._mean_effect(self._records(strategy_id))
        if effect is None or hypothesis_delta == 0 or (effect > 0) != (hypothesis_delta > 0) or effect == 0:
            return float(baseline) + hypothesis_delta
        return float(baseline) + effect

    def recommend(self, *, target_type: str, target_id: str, metric_name: str, now: float | None = None) -> dict:
        now = _time.time() if now is None else now
        scope = f"{target_type}:{target_id}"
        candidates = [s for s in self._strategies.values()
                      if (s.target_type, s.target_id, s.metric_name) == (target_type, target_id, metric_name)]
        assessment = self.gate.assess([EvidenceRequirement(target_type, target_id, metric_name, "estado atual")],
                                      now=now)
        regime = self._regime_check(candidates, target_type, target_id, metric_name, now)
        if not assessment.allowed:
            action, chosen, why = Recommendation.ABSTAIN, None, "estado atual sem evidência factual vigente"
        elif regime is not None:
            action, chosen, why = regime
        elif any(s.causal_status == CausalStatus.CAUSAL_SUPPORTED and s.status == StrategyStatus.VALIDATED
                 for s in candidates):
            best = max((s for s in candidates if s.causal_status == CausalStatus.CAUSAL_SUPPORTED),
                       key=lambda s: self._mean_effect(self._records(s.strategy_id)) or 0.0)
            action, chosen, why = Recommendation.SCALE, best.strategy_id, "efeito validado com grupo de controle"
        elif any(s.causal_status == CausalStatus.CORRELATIONAL_ONLY for s in candidates):
            best = next(s for s in candidates if s.causal_status == CausalStatus.CORRELATIONAL_ONLY)
            action, chosen = Recommendation.TEST_WITH_CONTROL, best.strategy_id
            why = "só correlação validada — escalar exige teste com controle"
        elif any(s.status == StrategyStatus.TESTING for s in candidates):
            best = next(s for s in candidates if s.status == StrategyStatus.TESTING)
            action, chosen, why = Recommendation.CONTINUE_TEST, best.strategy_id, "amostra real ainda insuficiente"
        elif candidates and all(s.status == StrategyStatus.ABANDONED for s in candidates):
            action, chosen, why = Recommendation.ABANDON, None, "todas as estratégias testadas foram refutadas"
        else:
            action, chosen, why = Recommendation.ABSTAIN, None, "nenhuma estratégia testada — sem base para agir"
        status = DecisionStatus.DECIDED if action in (Recommendation.SCALE, Recommendation.TEST_WITH_CONTROL,
                                                      Recommendation.CONTINUE_TEST, Recommendation.REVALIDATE) \
            else DecisionStatus.ABSTAINED
        decision = self.ledger.record(
            topic=f"recommendation:{metric_name}", scope=scope, status=status, chosen=chosen, actor=self.actor,
            justification=f"{action.value}: {why}",
            alternatives=[f"{s.strategy_id}={s.status.value}/{s.causal_status.value}" for s in candidates],
            evidence=assessment.fact_ids() + assessment.reasons(),
            uncertainty="" if action == Recommendation.SCALE else why, now=now,
        )
        return {"action": action, "strategy_id": chosen, "why": why, "decision_id": decision.decision_id}

    # ------------------------------------------ COMANDO 4/6: mudança de regime
    REGIME_MIN_RELATIVE_CHANGE = 0.25

    def _regime_check(self, candidates, target_type, target_id, metric_name, now):
        """Estratégia VALIDADA só governa a decisão enquanto o regime em
        que foi validada persistir. Mudança relevante do alvo DEPOIS da
        validação: PERSISTENTE => NEEDS_REVALIDATION (+ decisão
        registrada) e REVALIDATE; NÃO CONFIRMADA => HOLD (não escalar às
        cegas, mas também não declarar mudança real); PONTUAL => ignora."""
        from atlas.digital_twin import ChangeKind, detect_changes

        validated = [s for s in candidates if s.status in (StrategyStatus.VALIDATED, StrategyStatus.NEEDS_REVALIDATION)]
        for s in validated:
            validated_at = self._validated_at(s)
            changes = detect_changes(self.twin, target_type, target_id, metric_name,
                                     min_relative_change=self.REGIME_MIN_RELATIVE_CHANGE, since=validated_at)
            persistent = [c for c in changes if c.kind == ChangeKind.PERSISTENT]
            if persistent or s.status == StrategyStatus.NEEDS_REVALIDATION:
                if s.status != StrategyStatus.NEEDS_REVALIDATION:
                    c = persistent[-1]
                    s.status, s.updated_at = StrategyStatus.NEEDS_REVALIDATION, now
                    self._persist(s)
                    self.ledger.record(
                        topic=f"strategy:{s.strategy_id}", scope=s.scope_key, status=DecisionStatus.DECIDED,
                        chosen="NEEDS_REVALIDATION", actor=self.actor, now=now,
                        justification=(f"regime mudou de forma PERSISTENTE após a validação: {metric_name} "
                                       f"{c.from_value} -> {c.to_value} ({c.relative_change:+.0%}); "
                                       f"validação antiga não governa a decisão atual"),
                        evidence=list(c.evidence_fact_ids), uncertainty=c.note,
                    )
                return (Recommendation.REVALIDATE, s.strategy_id,
                        "regime de mercado mudou de forma persistente após a validação — revalidar antes de agir")
            if any(c.kind == ChangeKind.UNCONFIRMED for c in changes):
                return (Recommendation.HOLD, s.strategy_id,
                        "variação relevante observada UMA vez após a validação — não confirmada; escala suspensa")
        return None

    def _validated_at(self, s: Strategy) -> float:
        decision = next((d for d in reversed(self.ledger.history(topic=f"strategy:{s.strategy_id}", scope=s.scope_key))
                         if d.chosen == "VALIDATED"), None)
        return decision.created_at if decision else s.updated_at

    # ------------------------------------------------------------ auxiliares
    def get(self, strategy_id: str) -> Strategy | None:
        return self._strategies.get(strategy_id)

    def strategies(self) -> list:
        return list(self._strategies.values())

    def _get(self, strategy_id: str) -> Strategy:
        s = self._strategies.get(strategy_id)
        if s is None:
            raise KeyError(f"estratégia desconhecida: {strategy_id}")
        return s

    def _records(self, strategy_id: str) -> list:
        return [r for r in self.loop.all_records() if r.scope_project_id == strategy_id]

    @staticmethod
    def _mean_effect(records: list) -> float | None:
        effects = [r.actual_value - r.baseline_value for r in records if r.baseline_value is not None]
        return sum(effects) / len(effects) if effects else None

    def _persist(self, s: Strategy) -> None:
        if not self.db_path:
            return
        design = s.design.value + (f":{s.control_id}" if s.control_id else "")
        with _db_module.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO atlas_strategies (strategy_id, scope_channel_id, metric_name, hypothesis, "
                "test_design, status, causal_status, hypothesis_fact_id, predictions_json, last_evaluation, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (s.strategy_id, s.scope_key, s.metric_name, s.hypothesis, design, s.status.value,
                 s.causal_status.value, s.hypothesis_fact_id, _json.dumps(s.rounds), s.last_evaluation,
                 s.created_at, s.updated_at),
            )

    def _load(self) -> None:
        with _db_module.connect(self.db_path) as conn:
            for r in conn.execute("SELECT * FROM atlas_strategies").fetchall():
                design, _, control = r["test_design"].partition(":")
                target_type, _, target_id = r["scope_channel_id"].partition(":")
                self._strategies[r["strategy_id"]] = Strategy(
                    strategy_id=r["strategy_id"], target_type=target_type, target_id=target_id,
                    metric_name=r["metric_name"], hypothesis=r["hypothesis"], design=TestDesign(design),
                    control_id=control or None, status=StrategyStatus(r["status"]),
                    causal_status=CausalStatus(r["causal_status"]), hypothesis_fact_id=r["hypothesis_fact_id"],
                    rounds=_json.loads(r["predictions_json"]), last_evaluation=r["last_evaluation"],
                    created_at=r["created_at"], updated_at=r["updated_at"],
                )
