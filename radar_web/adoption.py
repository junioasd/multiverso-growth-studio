"""
radar_web.adoption
======================
Pipeline de adoção comercial:

    DISCOVERY -> CLASSIFICATION -> EVIDENCE_COLLECTION -> ANALYSIS
    -> HYPOTHESIS -> SENTINEL_QUALITY_GATE -> CONTROLLED_TEST
    -> METRICS -> [METRIC_UNVERIFIED ->] VALIDATION
    -> ADOPTED / RETEST / REJECTED

CORREÇÃO (item 1 desta rodada — MetricEvidence inválida nunca chega a
ADOPTED): `record_metrics()` agora VALIDA cada métrica protegida por
completo (resolve evidence_ids -> existência -> status -> binding com
a métrica -> binding com a estratégia) ANTES de aceitar — se qualquer
métrica crítica falhar, o registro vai para o novo estado explícito
`METRIC_UNVERIFIED` (nunca `METRICS`/`VALIDATION`/`ADOPTED`
silenciosamente). `METRIC_UNVERIFIED` só pode seguir para `RETEST`.

CORREÇÃO (item 7 — validate() nunca aceita ADOPTED cegamente):
`validate(decision="ADOPTED")` revalida o veredito do Quality Gate E
cada métrica protegida (evidência + binding) antes de aceitar — nunca
confia apenas na decisão do chamador.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum

from core.quality.gates import GateResult, Verdict


class AdoptionStatus(str, Enum):
    DISCOVERY = "DISCOVERY"
    CLASSIFICATION = "CLASSIFICATION"
    EVIDENCE_COLLECTION = "EVIDENCE_COLLECTION"
    ANALYSIS = "ANALYSIS"
    HYPOTHESIS = "HYPOTHESIS"
    SENTINEL_QUALITY_GATE = "SENTINEL_QUALITY_GATE"
    CONTROLLED_TEST = "CONTROLLED_TEST"
    METRICS = "METRICS"
    # CORREÇÃO (item 1): estado explícito — métrica crítica
    # apresentada mas que NÃO passou na validação completa. Nunca
    # avança para VALIDATION/ADOPTED a partir daqui — só RETEST.
    METRIC_UNVERIFIED = "METRIC_UNVERIFIED"
    VALIDATION = "VALIDATION"
    ADOPTED = "ADOPTED"
    REJECTED = "REJECTED"
    RETEST = "RETEST"


_TERMINAL = {AdoptionStatus.ADOPTED, AdoptionStatus.REJECTED}

_FORWARD_TRANSITIONS: dict = {
    AdoptionStatus.DISCOVERY: {AdoptionStatus.CLASSIFICATION},
    AdoptionStatus.CLASSIFICATION: {AdoptionStatus.EVIDENCE_COLLECTION},
    AdoptionStatus.EVIDENCE_COLLECTION: {AdoptionStatus.ANALYSIS},
    AdoptionStatus.ANALYSIS: {AdoptionStatus.HYPOTHESIS},
    AdoptionStatus.HYPOTHESIS: {AdoptionStatus.SENTINEL_QUALITY_GATE},
    AdoptionStatus.SENTINEL_QUALITY_GATE: {AdoptionStatus.CONTROLLED_TEST, AdoptionStatus.REJECTED},
    AdoptionStatus.CONTROLLED_TEST: {AdoptionStatus.METRICS},
    AdoptionStatus.METRICS: {AdoptionStatus.VALIDATION, AdoptionStatus.METRIC_UNVERIFIED},
    AdoptionStatus.METRIC_UNVERIFIED: {AdoptionStatus.RETEST},
    AdoptionStatus.VALIDATION: {AdoptionStatus.ADOPTED, AdoptionStatus.REJECTED, AdoptionStatus.RETEST},
    AdoptionStatus.RETEST: {AdoptionStatus.CONTROLLED_TEST},
    AdoptionStatus.ADOPTED: set(),
    AdoptionStatus.REJECTED: set(),
}


class InvalidAdoptionTransition(Exception):
    pass


class QualityGateRejection(Exception):
    """Levantado quando o Quality Gate do SENTINEL bloqueia/rejeita a
    estratégia — nunca avança silenciosamente para CONTROLLED_TEST."""


class MetricValidationFailed(ValueError):
    """CORREÇÃO (item 1): levantado quando qualquer métrica protegida
    não passa na validação completa — o registro é movido para
    `METRIC_UNVERIFIED`, nunca finge sucesso. Herda de `ValueError`
    para compatibilidade com qualquer chamador que já capturava
    `ValueError` genericamente."""


class AdoptionValidationFailed(Exception):
    """CORREÇÃO (item 7): levantado quando `validate(decision="ADOPTED")`
    é recusado após revalidação — nunca aceita a decisão do chamador
    cegamente."""


@dataclass
class AdoptionRecord:
    record_id: str
    strategy_id: str
    status: AdoptionStatus = AdoptionStatus.DISCOVERY
    strategy: object = None
    hypothesis: str | None = None
    evidence: dict = field(default_factory=dict)
    quality_gate_result: dict | None = None
    metrics: dict = field(default_factory=dict)
    metrics_provenance: object = None
    metric_validation_issues: list = field(default_factory=list)
    notes: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    @staticmethod
    def new_id() -> str:
        return f"adopt_{uuid.uuid4().hex[:12]}"


class AdoptionPipeline:
    def __init__(self, *, quality_gate=None) -> None:
        from radar_web.quality_gate import RadarWebEvidenceGate

        self._records: dict = {}
        self.quality_gate = quality_gate if quality_gate is not None else RadarWebEvidenceGate()

    def start(self, strategy_id: str) -> AdoptionRecord:
        record = AdoptionRecord(record_id=AdoptionRecord.new_id(), strategy_id=strategy_id)
        self._records[record.record_id] = record
        return record

    def get(self, record_id: str) -> AdoptionRecord:
        if record_id not in self._records:
            raise KeyError(record_id)
        return self._records[record_id]

    def _advance(self, record_id: str, *, to: AdoptionStatus, **updates) -> AdoptionRecord:
        record = self.get(record_id)
        if record.status in _TERMINAL:
            raise InvalidAdoptionTransition(f"registro '{record_id}' já está em estado terminal ({record.status.value})")
        allowed = _FORWARD_TRANSITIONS.get(record.status, set())
        if to not in allowed:
            raise InvalidAdoptionTransition(
                f"não é possível ir de '{record.status.value}' para '{to.value}' — permitido: {[s.value for s in allowed]}"
            )
        for key, value in updates.items():
            setattr(record, key, value)
        record.status = to
        record.updated_at = time.time()
        return record

    def classify(self, record_id: str, *, notes: str = "") -> AdoptionRecord:
        return self._advance(record_id, to=AdoptionStatus.CLASSIFICATION, notes=notes)

    def collect_evidence(self, record_id: str, *, evidence: dict) -> AdoptionRecord:
        if not evidence:
            raise ValueError("coleta de evidência exige ao menos um item — nunca avança sem evidência real")
        return self._advance(record_id, to=AdoptionStatus.EVIDENCE_COLLECTION, evidence=evidence)

    def analyze(self, record_id: str, *, notes: str = "") -> AdoptionRecord:
        return self._advance(record_id, to=AdoptionStatus.ANALYSIS, notes=notes)

    def propose_hypothesis(self, record_id: str, *, hypothesis: str) -> AdoptionRecord:
        return self._advance(record_id, to=AdoptionStatus.HYPOTHESIS, hypothesis=hypothesis)

    def run_quality_gate(self, record_id: str, *, strategy, metrics: dict | None = None) -> AdoptionRecord:
        """Etapa OBRIGATÓRIA — avalia a estratégia através do
        `RadarWebEvidenceGate` real. `REJECTED`/`BLOCKED` NUNCA avança
        para `CONTROLLED_TEST`. A `strategy` fica armazenada no
        registro — necessária para revalidação de binding
        estratégia↔evidência em `record_metrics()`/`validate()`."""
        gate_result: GateResult = self.quality_gate.evaluate({"strategy": strategy, "metrics": metrics or {}})
        result_dict = {
            "verdict": gate_result.verdict.value, "reasons": gate_result.reasons, "criteria": gate_result.criteria,
        }
        self._advance(record_id, to=AdoptionStatus.SENTINEL_QUALITY_GATE, quality_gate_result=result_dict, strategy=strategy)
        if gate_result.verdict in (Verdict.REJECTED, Verdict.BLOCKED):
            self._advance(
                record_id, to=AdoptionStatus.REJECTED,
                notes=f"rejeitado pelo Quality Gate: {'; '.join(gate_result.reasons)}",
            )
            raise QualityGateRejection(f"Quality Gate recusou a estratégia: {gate_result.reasons}")
        return self._advance(record_id, to=AdoptionStatus.CONTROLLED_TEST)

    def _validate_protected_metrics(self, record: AdoptionRecord, metrics: dict) -> list:
        """CORREÇÃO (item 11): reaproveita o MESMO
        `MetricEvidenceValidator` canônico usado pelo Quality Gate —
        nunca uma lógica de validação divergente aqui."""
        from core.evidence.claims import requires_verifiable_source
        from core.evidence.metric_evidence_validator import MetricEvidenceValidator

        validator = MetricEvidenceValidator(evidence_registry=self.quality_gate.evidence_registry)
        issues = []
        for key, value in metrics.items():
            if not requires_verifiable_source(key):
                continue
            outcome = validator.validate(requested_key=key, metric_evidence=value, strategy_id=record.strategy_id)
            if not outcome.is_valid:
                issues.extend(outcome.issues)
        return issues

    def record_metrics(self, record_id: str, *, metrics: dict, provenance) -> AdoptionRecord:
        """`provenance` aceita string livre (legado — nunca suficiente
        SOZINHA para métricas comerciais protegidas) ou
        `core.evidence.provenance.ProvenanceRecord` estruturado e
        validável.

        CORREÇÃO (item 1): cada métrica comercial PROTEGIDA é
        VALIDADA POR COMPLETO (não só "é MetricEvidence?") antes de
        aceitar — se qualquer uma falhar (evidência MISSING/
        UNVERIFIED/INVALID/EXPIRED ou binding incorreto), o registro
        vai para `METRIC_UNVERIFIED` (nunca `METRICS`/`VALIDATION`),
        e `MetricValidationFailed` é levantada."""
        from core.evidence.provenance import ProvenanceRecord

        if not metrics:
            raise ValueError("métricas exigidas — nunca avançar sem dado real do teste controlado")

        if isinstance(provenance, ProvenanceRecord):
            if not provenance.is_structurally_valid():
                raise ValueError(
                    "ProvenanceRecord incompleto — source_type/source_reference/collection_method/collector/"
                    "observed_at/collected_at são todos obrigatórios"
                )
        elif isinstance(provenance, str):
            if not provenance.strip():
                raise ValueError("métricas exigem `provenance` explícita — nunca aceitas sem origem declarada")
        else:
            raise ValueError("`provenance` precisa ser uma string ou um ProvenanceRecord estruturado")

        record = self.get(record_id)
        issues = self._validate_protected_metrics(record, metrics)
        if issues:
            self._advance(record_id, to=AdoptionStatus.METRICS, metrics=metrics, metrics_provenance=provenance)
            self._advance(record_id, to=AdoptionStatus.METRIC_UNVERIFIED, metric_validation_issues=issues)
            raise MetricValidationFailed(f"métrica(s) crítica(s) não comprovada(s): {issues}")

        return self._advance(
            record_id, to=AdoptionStatus.METRICS, metrics=metrics, metrics_provenance=provenance,
            metric_validation_issues=[],
        )

    def validate(self, record_id: str, *, decision: str, notes: str = "") -> AdoptionRecord:
        """CORREÇÃO (item 7): nunca aceita `decision="ADOPTED"`
        cegamente — revalida o veredito do Quality Gate E cada
        métrica protegida (evidência + binding) antes de aceitar.
        Falha na revalidação -> `AdoptionValidationFailed`, o
        registro NUNCA chega a `ADOPTED`."""
        try:
            target = AdoptionStatus(decision)
        except ValueError:
            raise ValueError(f"decisão inválida: '{decision}' — esperado ADOPTED, REJECTED ou RETEST")
        if target not in (AdoptionStatus.ADOPTED, AdoptionStatus.REJECTED, AdoptionStatus.RETEST):
            raise ValueError(f"decisão inválida: '{decision}' — esperado ADOPTED, REJECTED ou RETEST")

        record = self.get(record_id)
        if target == AdoptionStatus.ADOPTED:
            gate_verdict = (record.quality_gate_result or {}).get("verdict")
            if gate_verdict not in (Verdict.APPROVED.value, Verdict.REVIEW_REQUIRED.value):
                self._advance(record_id, to=AdoptionStatus.VALIDATION)
                self._advance(
                    record_id, to=AdoptionStatus.REJECTED,
                    notes=f"ADOPTED recusado na revalidação: Quality Gate não aprovou (verdict={gate_verdict})",
                )
                raise AdoptionValidationFailed(f"ADOPTED recusado: Quality Gate verdict={gate_verdict}")

            issues = self._validate_protected_metrics(record, record.metrics or {})
            if issues:
                self._advance(record_id, to=AdoptionStatus.VALIDATION)
                self._advance(
                    record_id, to=AdoptionStatus.REJECTED,
                    notes=f"ADOPTED recusado na revalidação: métricas não comprovadas: {issues}",
                )
                raise AdoptionValidationFailed(f"ADOPTED recusado: métricas não comprovadas: {issues}")

        self._advance(record_id, to=AdoptionStatus.VALIDATION)
        return self._advance(record_id, to=target, notes=notes)

    def retest(self, record_id: str) -> AdoptionRecord:
        """Retoma para `CONTROLLED_TEST`. A partir de `METRIC_UNVERIFIED`
        (item 1), passa primeiro por `RETEST` (etapa explícita do
        pipeline) antes de voltar a `CONTROLLED_TEST` — mesma
        disciplina de transição em dois saltos já usada em outras
        etapas deste pipeline."""
        record = self.get(record_id)
        if record.status == AdoptionStatus.METRIC_UNVERIFIED:
            self._advance(record_id, to=AdoptionStatus.RETEST)
        return self._advance(record_id, to=AdoptionStatus.CONTROLLED_TEST)

    @staticmethod
    def prefer_own_data(*, own_metrics: dict | None, market_inference: dict | None) -> dict:
        """Item 6 (rodada anterior): "Resultado próprio validado deve
        ter prioridade sobre inferência de mercado quando houver dados
        suficientes." Dado próprio (validado via teste controlado)
        SEMPRE prevalece quando presente — inferência de mercado só é
        usada na ausência total de dado próprio."""
        if own_metrics:
            return dict(own_metrics)
        return dict(market_inference or {})
