"""
core.evidence.metric_evidence_validator
===========================================
CORREÇÃO (item 11 — MetricEvidenceValidator centralizado).

Validador ÚNICO usado por TODOS os pontos do pipeline que precisam
avaliar uma `MetricEvidence` (Quality Gate, `record_metrics()`,
`validate()`, promoção à Biblioteca Mestre) — evita validações
divergentes em pontos distintos.

CORREÇÃO (item 4 — ProvenanceRecord obrigatória para FACT): uma
métrica comercial protegida SEM `ProvenanceRecord` (ou com uma
`ProvenanceRecord` que não resolve para `VERIFIED` via
`grant_verification()`) é SEMPRE `UNVERIFIED` — nunca `FACT`, nunca
sustenta `ADOPTED`.

CORREÇÃO (item 7 — binding TRIPLO obrigatório): após canonicalização,
os três valores — chave solicitada (`requested_key`),
`MetricEvidence.metric_name`, e `EvidenceRecord.related_metric_name`
de CADA evidência vinculada — precisam ser IGUAIS. Qualquer
divergência é `METRIC_BINDING_ERROR`, nunca aprovado.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.evidence.registry import CanonicalMetricRegistry, EvidenceRegistry, VerificationStatus

_STATUS_PRIORITY = [
    VerificationStatus.MISSING, VerificationStatus.INVALID, VerificationStatus.EXPIRED,
    VerificationStatus.UNVERIFIED, VerificationStatus.VERIFIED,
]


def _worse(a: VerificationStatus, b: VerificationStatus) -> VerificationStatus:
    return a if _STATUS_PRIORITY.index(a) < _STATUS_PRIORITY.index(b) else b


@dataclass
class MetricValidationOutcome:
    status: VerificationStatus
    issues: list = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        return self.status == VerificationStatus.VERIFIED and not self.issues


class MetricEvidenceValidator:
    """Validador canônico ÚNICO — todo ponto do pipeline que precisa
    decidir se uma `MetricEvidence` sustenta `FACT` deve passar por
    aqui, nunca reimplementar a lógica em outro lugar."""

    def __init__(self, *, evidence_registry: EvidenceRegistry) -> None:
        self.evidence_registry = evidence_registry

    def validate(self, *, requested_key: str, metric_evidence, strategy_id: str | None = None) -> MetricValidationOutcome:
        """Valida uma `MetricEvidence` por completo: canonical metric,
        binding TRIPLO, existência/status de cada evidência vinculada,
        `ProvenanceRecord` OBRIGATÓRIA e VERIFIED, contexto e tipo de
        medição preenchidos."""
        from core.evidence.metric_evidence import MetricEvidence

        issues: list = []

        if not isinstance(metric_evidence, MetricEvidence):
            return MetricValidationOutcome(
                status=VerificationStatus.UNVERIFIED,
                issues=[f"{requested_key}: valor numérico solto (sem MetricEvidence) — nunca válido"],
            )

        canonical_requested = CanonicalMetricRegistry.canonicalize(requested_key)
        canonical_me = CanonicalMetricRegistry.canonicalize(metric_evidence.metric_name)
        if canonical_requested != canonical_me:
            issues.append(
                f"METRIC_BINDING_ERROR: chave solicitada '{canonical_requested}' != "
                f"MetricEvidence.metric_name '{canonical_me}'"
            )

        if not metric_evidence.context or not metric_evidence.measurement_type:
            issues.append(f"{requested_key}: context/measurement_type ausente")

        # CORREÇÃO (item 4): ProvenanceRecord OBRIGATÓRIA para métrica
        # protegida — nunca opcional. Precisa estar genuinamente
        # VERIFIED (via grant_verification, nunca declarativa).
        provenance_record = getattr(metric_evidence, "provenance_record", None)
        if provenance_record is None:
            issues.append(f"{requested_key}: ProvenanceRecord ausente — obrigatória para métrica protegida usada como FACT")
        else:
            pr_status = provenance_record.grant_verification(
                verifier_registry=self.evidence_registry.verifier_registry,
                source_registry=self.evidence_registry.source_registry,
                evidence_registry=self.evidence_registry,
            )
            if pr_status != VerificationStatus.VERIFIED:
                reason = getattr(provenance_record, "_last_denial_reason", None)
                detail = f" ({reason})" if reason else ""
                issues.append(f"{requested_key}: ProvenanceRecord não VERIFIED (status={pr_status.value}){detail}")
            elif strategy_id is not None and provenance_record.related_strategy_id is not None:
                # CORREÇÃO (item 11 — PROVENANCE_STRATEGY_MISMATCH):
                # ProvenanceRecord de outra estratégia nunca sustenta a
                # estratégia sendo avaliada, a menos que explicitamente
                # GLOBAL/BENCHMARK/CROSS_STRATEGY.
                if provenance_record.related_strategy_id != strategy_id and provenance_record.scope == "STRATEGY_SPECIFIC":
                    issues.append(
                        f"PROVENANCE_STRATEGY_MISMATCH: ProvenanceRecord vinculada à estratégia "
                        f"'{provenance_record.related_strategy_id}', não a '{strategy_id}'"
                    )
                elif (
                    provenance_record.related_claim_id is not None
                    and self.evidence_registry.claim_registry.resolve(provenance_record.related_claim_id) is not None
                    and not self.evidence_registry.claim_registry.matches(
                        provenance_record.related_claim_id, strategy_id=strategy_id,
                    )
                ):
                    # CORREÇÃO (item 12): claim da ProvenanceRecord
                    # também precisa bater com a estratégia avaliada.
                    issues.append(
                        f"CLAIM_BINDING_ERROR: ProvenanceRecord vinculada à claim "
                        f"'{provenance_record.related_claim_id}', que não pertence à estratégia '{strategy_id}'"
                    )

        if not metric_evidence.evidence_ids:
            issues.append(f"{requested_key}: nenhum evidence_id vinculado")
            return MetricValidationOutcome(status=VerificationStatus.MISSING, issues=issues)

        worst = VerificationStatus.VERIFIED
        for eid in metric_evidence.evidence_ids:
            # CORREÇÃO (item 7): binding TRIPLO — o terceiro vértice
            # (EvidenceRecord.related_metric_name) é checado por
            # `resolve_status_for`, que já compara com a métrica
            # SOLICITADA (não a declarada pela MetricEvidence — os
            # dois já foram conferidos serem iguais acima).
            status, binding_error = self.evidence_registry.resolve_status_for(
                eid, strategy_id=strategy_id, metric_name=requested_key,
            )
            if binding_error:
                issues.append(binding_error)
            elif status != VerificationStatus.VERIFIED:
                # CORREÇÃO (bug real encontrado nesta rodada): mesmo
                # sem um binding_error específico, um status que não é
                # VERIFIED (MISSING/UNVERIFIED/INVALID/EXPIRED) SEMPRE
                # precisa gerar uma mensagem explícita — nunca deixar a
                # lista de issues vazia enquanto o status reprova (o
                # chamador decide se algo falhou olhando `issues`, não
                # só `status`).
                issues.append(f"{requested_key}: evidência '{eid}' com status {status.value} — não comprovada")
            worst = _worse(worst, status)

        if issues and worst == VerificationStatus.VERIFIED:
            worst = VerificationStatus.INVALID
        return MetricValidationOutcome(status=worst, issues=issues)
