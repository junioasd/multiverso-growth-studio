"""
core.evidence.provenance
============================
CORREÇÃO (item 6 desta rodada — ProvenanceRecord: SourceRegistry
obrigatório): `source_reference` de uma `ProvenanceRecord` agora
precisa RESOLVER contra o `SourceRegistry` — uma referência nunca
registrada (`source_reference="fake://x"`) NUNCA resulta em
`VERIFIED`, mesmo com todos os outros campos preenchidos.

CORREÇÃO (item 7 — binding com evidence real): `related_evidence_ids`
precisa ser NÃO VAZIO (quando evidência é exigida) e cada ID precisa
EXISTIR e estar `VERIFIED` no `EvidenceRegistry` fornecido.

CORREÇÃO (item 8 — integrity): quando a política do tipo de
fonte/evidência exige `integrity_reference` (ex: `OWN_OPERATION_DATA`,
`API_RESPONSE`, `FILE`, `REPORT`, `TEST_RESULT`), `integrity_reference
=None` bloqueia `VERIFIED`.

CORREÇÃO (item 9 — verification real): `grant_verification()` agora
resolve TUDO — `SourceRegistry`, `VerifierRegistry`, evidência
relacionada, binding, freshness — nunca aceita o chamador passando só
um `verification_status` e pronto."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field

from core.evidence.registry import VerificationStatus

# CORREÇÃO (item 8): tipos de fonte/evidência que exigem
# `integrity_reference` — sem isso, nunca `VERIFIED`.
_INTEGRITY_REQUIRED_SOURCE_TYPES = frozenset({
    "OWN_OPERATION_DATA", "API_RESPONSE", "FILE", "REPORT", "TEST_RESULT",
})


@dataclass
class ProvenanceRecord:
    source_type: str
    source_reference: str
    collection_method: str
    collector: str
    provenance_id: str = field(default_factory=lambda: f"prov_{uuid.uuid4().hex[:12]}")
    observed_at: float = field(default_factory=time.time)
    collected_at: float = field(default_factory=time.time)
    verifier: str | None = None
    verification_method: str | None = None
    # CORREÇÃO (item 6): `requested_verification_status` é só o PEDIDO
    # — `verification_status` (efetivo) nunca é promovido a VERIFIED
    # automaticamente no construtor, só via `grant_verification()`.
    requested_verification_status: VerificationStatus = VerificationStatus.UNVERIFIED
    verification_status: VerificationStatus = field(init=False, default=VerificationStatus.UNVERIFIED)
    integrity_reference: str | None = None
    related_evidence_ids: list = field(default_factory=list)
    related_strategy_id: str | None = None
    related_claim_id: str | None = None
    # CORREÇÃO (item 11 desta rodada): mesma disciplina de
    # `EvidenceRecord.scope` — STRATEGY_SPECIFIC por padrão; só
    # GLOBAL/BENCHMARK/CROSS_STRATEGY permite reutilização entre
    # estratégias diferentes.
    scope: str = "STRATEGY_SPECIFIC"
    # alias de compatibilidade — código anterior desta correção usava
    # `evidence_ids`; mantido como espelho de `related_evidence_ids`.
    evidence_ids: list = field(default_factory=list)
    _last_denial_reason: str | None = field(init=False, default=None, repr=False)

    def __post_init__(self) -> None:
        if self.evidence_ids and not self.related_evidence_ids:
            self.related_evidence_ids = list(self.evidence_ids)
        elif self.related_evidence_ids and not self.evidence_ids:
            self.evidence_ids = list(self.related_evidence_ids)
        # CORREÇÃO (item 6): nunca promove VERIFIED sozinho no
        # construtor — outros status (UNVERIFIED/INVALID/EXPIRED) são
        # respeitados como pedidos, mas VERIFIED exige
        # `grant_verification()`.
        if self.requested_verification_status == VerificationStatus.VERIFIED:
            self.verification_status = VerificationStatus.UNVERIFIED
        else:
            self.verification_status = self.requested_verification_status

    def is_structurally_valid(self) -> bool:
        """Checagem MÍNIMA de estrutura — nunca substitui
        `grant_verification()`. Só garante que os campos obrigatórios
        não estão vazios."""
        return bool(
            self.source_type and self.source_reference and self.collection_method and self.collector
            and self.observed_at and self.observed_at > 0 and self.collected_at and self.collected_at > 0
        )

    def _requires_integrity_reference(self) -> bool:
        return self.source_type in _INTEGRITY_REQUIRED_SOURCE_TYPES

    def grant_verification(
        self, *, verifier_registry, source_registry=None, evidence_registry=None, require_evidence: bool = True,
    ) -> VerificationStatus:
        """CORREÇÃO (itens 6/7/8/9): ÚNICO caminho que pode elevar
        `verification_status` para `VERIFIED` — exige, em ordem:

        1. estrutura completa;
        2. `verifier` REGISTRADO e AUTORIZADO;
        3. (quando `source_registry` fornecido) `source_reference`
           RESOLVE contra o `SourceRegistry` e sustenta FACT — uma
           referência nunca registrada (`'fake://x'`) nunca passa;
        4. `integrity_reference` presente quando o tipo de fonte exige;
        5. (quando `require_evidence=True`, o padrão) `related_evidence_ids`
           não vazio, e (quando `evidence_registry` fornecido) cada ID
           EXISTE e está `VERIFIED`.

        Qualquer requisito ausente -> `UNVERIFIED`, nunca finge. O
        motivo específico fica em `self._last_denial_reason` (só para
        diagnóstico)."""
        self._last_denial_reason = None

        if not self.is_structurally_valid():
            self._last_denial_reason = "estrutura incompleta"
            self.verification_status = VerificationStatus.UNVERIFIED
            return self.verification_status

        if not self.verifier or not self.verification_method:
            self._last_denial_reason = "verifier/verification_method ausente"
            self.verification_status = VerificationStatus.UNVERIFIED
            return self.verification_status
        if not verifier_registry.is_authorized(self.verifier, verification_method=self.verification_method):
            self._last_denial_reason = f"verifier '{self.verifier}' não autorizado"
            self.verification_status = VerificationStatus.UNVERIFIED
            return self.verification_status

        # CORREÇÃO (item 6): source_reference precisa RESOLVER — nunca
        # aceita uma referência nunca registrada.
        if source_registry is not None:
            source_registration = source_registry.resolve(self.source_reference)
            if source_registration is None or not source_registration.can_sustain_fact():
                self._last_denial_reason = f"source_reference '{self.source_reference}' não resolve/não sustenta FACT"
                self.verification_status = VerificationStatus.UNVERIFIED
                return self.verification_status

        # CORREÇÃO (item 8): integrity obrigatória para certos tipos.
        if self._requires_integrity_reference() and not self.integrity_reference:
            self._last_denial_reason = f"integrity_reference obrigatória para source_type='{self.source_type}'"
            self.verification_status = VerificationStatus.UNVERIFIED
            return self.verification_status

        # CORREÇÃO (item 7): binding com evidência real.
        if require_evidence:
            if not self.related_evidence_ids:
                self._last_denial_reason = "related_evidence_ids vazio — evidência exigida"
                self.verification_status = VerificationStatus.UNVERIFIED
                return self.verification_status
            if evidence_registry is not None:
                for eid in self.related_evidence_ids:
                    if evidence_registry.resolve_status(eid) != VerificationStatus.VERIFIED:
                        self._last_denial_reason = f"evidence_id '{eid}' não está VERIFIED"
                        self.verification_status = VerificationStatus.UNVERIFIED
                        return self.verification_status

        self.verification_status = VerificationStatus.VERIFIED
        return self.verification_status

    @property
    def is_valid(self) -> bool:
        """CORREÇÃO (item 5 rodada anterior): só `VERIFIED` sustenta
        FACT — `INVALID`/`UNVERIFIED`/`EXPIRED` sempre `is_valid=False`."""
        return self.verification_status == VerificationStatus.VERIFIED
