"""
core.evidence.metric_evidence
=================================
CORREÇÃO (item 4 rodada anterior — provenance por métrica) +
CORREÇÃO (item 2 desta rodada — binding forte métrica↔MetricEvidence) +
CORREÇÃO (item 1 desta rodada — MetricEvidence inválida nunca chega a
ADOPTED).

`MetricEvidence` é a estrutura OBRIGATÓRIA para qualquer métrica
comercial protegida que pretenda ser tratada como `FACT` — um número
solto (`int`/`float`) NUNCA é aceito.

CORREÇÃO (item 2): `resolve_verification_detailed()` valida que CADA
`evidence_id` vinculado está genuinamente ligado à MESMA métrica
solicitada (via `core.evidence.registry.canonical_metric_name` —
aliases semanticamente equivalentes resolvem juntos, métricas
diferentes NUNCA se confundem) e à MESMA estratégia (quando
`strategy_id` é informado) — mismatch nunca aprovado silenciosamente,
sempre reportado como `binding_errors` explícito.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from core.evidence.registry import EvidenceRegistry, VerificationStatus, canonical_metric_name

_STATUS_PRIORITY = [
    VerificationStatus.MISSING, VerificationStatus.INVALID, VerificationStatus.EXPIRED,
    VerificationStatus.UNVERIFIED, VerificationStatus.VERIFIED,
]


@dataclass
class MetricVerificationResult:
    status: VerificationStatus
    binding_errors: list = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        return self.status == VerificationStatus.VERIFIED and not self.binding_errors


@dataclass
class MetricEvidence:
    metric_name: str
    value: float
    source: str
    provenance: str
    observed_at: float
    context: str
    measurement_type: str
    confidence: float
    evidence_ids: list = field(default_factory=list)
    unit: str | None = None
    sample_window: str | None = None
    # CORREÇÃO (item 5/6 — ProvenanceRecord estruturado opcional POR
    # métrica): quando fornecido, é validado estruturalmente pelo
    # Quality Gate antes de sustentar FACT — nunca aceito se inválido.
    provenance_record: object = None

    def resolve_verification_detailed(
        self, evidence_registry: EvidenceRegistry, *, strategy_id: str | None = None,
    ) -> MetricVerificationResult:
        """CORREÇÃO (itens 1/2): a métrica só é `VERIFIED` quando
        TODOS os `evidence_ids` vinculados resolvem para
        `VerificationStatus.VERIFIED` E estão genuinamente vinculados
        a ESTA métrica (nunca outra) e a ESTA estratégia (quando
        informada). Um único `MISSING`/`UNVERIFIED`/`INVALID`/
        `EXPIRED`/mismatch de binding já derruba a métrica inteira
        para o pior status encontrado — nunca "quase verificado"."""
        if not self.evidence_ids:
            return MetricVerificationResult(status=VerificationStatus.MISSING)

        worst = VerificationStatus.VERIFIED
        binding_errors: list = []
        for eid in self.evidence_ids:
            status, binding_error = evidence_registry.resolve_status_for(
                eid, strategy_id=strategy_id, metric_name=self.metric_name,
            )
            if binding_error:
                binding_errors.append(binding_error)
            if _STATUS_PRIORITY.index(status) < _STATUS_PRIORITY.index(worst):
                worst = status
        return MetricVerificationResult(status=worst, binding_errors=binding_errors)

    def resolve_verification(
        self, evidence_registry: EvidenceRegistry, *, strategy_id: str | None = None,
    ) -> VerificationStatus:
        """Compatibilidade — devolve só o status. Prefira
        `resolve_verification_detailed()` quando os `binding_errors`
        importam (relatórios/gates)."""
        return self.resolve_verification_detailed(evidence_registry, strategy_id=strategy_id).status
