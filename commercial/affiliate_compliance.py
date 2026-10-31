"""
commercial.affiliate_compliance
=================================
Affiliate Compliance Gate (Seção 38). Antes de promover produto,
verifica regras da plataforma, termos do produtor, restrições de
tráfego/palavras-chave, uso de marca/imagem/materiais, claims
permitidos, políticas da rede social, disclosure de afiliado,
requisitos legais. UNKNOWN crítico => BLOCK ou REVIEW_REQUIRED.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.quality.gates import GateResult, QualityGate, Verdict


@dataclass
class ComplianceCheckInput:
    platform_rules_reviewed: bool | None = None
    producer_terms_reviewed: bool | None = None
    traffic_restrictions_ok: bool | None = None
    keyword_restrictions_ok: bool | None = None
    brand_usage_authorized: bool | None = None
    image_usage_authorized: bool | None = None
    claims_within_policy: bool | None = None
    social_platform_policy_ok: bool | None = None
    affiliate_disclosure_present: bool | None = None
    legal_requirements_met: bool | None = None


class AffiliateComplianceGate(QualityGate):
    name = "affiliate_compliance_gate"

    _CRITICAL_FIELDS = [
        "platform_rules_reviewed",
        "producer_terms_reviewed",
        "claims_within_policy",
        "affiliate_disclosure_present",
        "legal_requirements_met",
    ]

    def evaluate(self, subject: dict) -> GateResult:
        data = subject if isinstance(subject, dict) else subject.__dict__
        reasons: list[str] = []
        criteria: dict[str, str] = {}
        verdict = Verdict.APPROVED

        order = [
            Verdict.BLOCKED, Verdict.REJECTED, Verdict.REWORK_REQUIRED,
            Verdict.REVIEW_REQUIRED, Verdict.UNKNOWN, Verdict.APPROVED,
        ]

        def worsen(new_verdict: Verdict) -> None:
            nonlocal verdict
            if order.index(new_verdict) < order.index(verdict):
                verdict = new_verdict

        for field_name in [
            "platform_rules_reviewed", "producer_terms_reviewed", "traffic_restrictions_ok",
            "keyword_restrictions_ok", "brand_usage_authorized", "image_usage_authorized",
            "claims_within_policy", "social_platform_policy_ok", "affiliate_disclosure_present",
            "legal_requirements_met",
        ]:
            value = data.get(field_name)
            is_critical = field_name in self._CRITICAL_FIELDS

            if value is False:
                criteria[field_name] = "FAIL"
                reasons.append(f"{field_name}: não conforme")
                worsen(Verdict.BLOCKED)
            elif value is None:
                criteria[field_name] = "UNKNOWN"
                reasons.append(f"{field_name}: não verificado")
                worsen(Verdict.BLOCKED if is_critical else Verdict.REVIEW_REQUIRED)
            else:
                criteria[field_name] = "OK"

        return GateResult(verdict=verdict, reasons=reasons, criteria=criteria)
