"""
core.quality.commercial_quality
=================================
Commercial Quality Gate (Seção 50). Antes de escalar produto/oferta,
avalia cliques, leads, vendas, conversão, refund, receita, margem,
reclamações, qualidade, compliance, audience fit, persistência.

Uma venda isolada NÃO comprova escalabilidade — exige amostra mínima.
"""

from __future__ import annotations

from core.quality.gates import GateResult, QualityGate, Verdict

MIN_SAMPLE_FOR_SCALING = 30       # cliques mínimos antes de considerar escalar
MIN_LEADS_FOR_SCALING = 10
MAX_REFUND_RATE = 0.15
MIN_CONVERSION_RATE = 0.005       # 0.5%


class CommercialQualityGate(QualityGate):
    name = "commercial_quality_gate"

    def evaluate(self, subject: dict) -> GateResult:
        """`subject` esperado:
        {
          "clicks": int, "leads": int, "sales": int,
          "conversion_rate": float, "refund_rate": float,
          "revenue": float, "margin": float,
          "complaints": int, "audience_fit_score": float (0-1),
          "compliance_status": "OK"|"UNKNOWN"|"VIOLATION",
          "persistent_over_days": int,
        }
        """
        reasons: list[str] = []
        criteria: dict[str, str] = {}
        verdict = Verdict.APPROVED

        def worsen(new_verdict: Verdict, reason: str, key: str, status: str) -> None:
            nonlocal verdict
            order = [
                Verdict.BLOCKED, Verdict.REJECTED, Verdict.REWORK_REQUIRED,
                Verdict.REVIEW_REQUIRED, Verdict.UNKNOWN, Verdict.APPROVED,
            ]
            reasons.append(reason)
            criteria[key] = status
            if order.index(new_verdict) < order.index(verdict):
                verdict = new_verdict

        compliance = subject.get("compliance_status", "UNKNOWN")
        if compliance == "VIOLATION":
            worsen(Verdict.BLOCKED, "violação de compliance identificada", "compliance", "FAIL")
        elif compliance == "UNKNOWN":
            worsen(Verdict.UNKNOWN, "compliance não verificado", "compliance", "UNKNOWN")
        else:
            criteria["compliance"] = "OK"

        clicks = subject.get("clicks")
        leads = subject.get("leads")
        if clicks is None or leads is None:
            worsen(Verdict.UNKNOWN, "dados de cliques/leads insuficientes", "sample_size", "UNKNOWN")
        elif clicks < MIN_SAMPLE_FOR_SCALING or leads < MIN_LEADS_FOR_SCALING:
            worsen(
                Verdict.REVIEW_REQUIRED,
                "amostra insuficiente para comprovar escalabilidade (venda isolada não conta)",
                "sample_size", "INSUFFICIENT",
            )
        else:
            criteria["sample_size"] = "OK"

        refund_rate = subject.get("refund_rate")
        if refund_rate is not None:
            if refund_rate > MAX_REFUND_RATE:
                worsen(Verdict.REJECTED, f"refund rate elevado: {refund_rate:.1%}", "refund_rate", "FAIL")
            else:
                criteria["refund_rate"] = "OK"
        else:
            worsen(Verdict.UNKNOWN, "refund_rate desconhecido", "refund_rate", "UNKNOWN")

        conversion_rate = subject.get("conversion_rate")
        if conversion_rate is not None:
            if conversion_rate < MIN_CONVERSION_RATE:
                worsen(Verdict.REWORK_REQUIRED, "conversão abaixo do mínimo aceitável", "conversion_rate", "FAIL")
            else:
                criteria["conversion_rate"] = "OK"
        else:
            worsen(Verdict.UNKNOWN, "conversion_rate desconhecido", "conversion_rate", "UNKNOWN")

        margin = subject.get("margin")
        if margin is not None:
            if margin <= 0:
                worsen(Verdict.REJECTED, "margem negativa ou nula", "margin", "FAIL")
            else:
                criteria["margin"] = "OK"
        else:
            worsen(Verdict.UNKNOWN, "margem desconhecida", "margin", "UNKNOWN")

        complaints = subject.get("complaints")
        if complaints and complaints > 0:
            worsen(Verdict.REVIEW_REQUIRED, f"reclamações registradas: {complaints}", "complaints", "REVIEW")

        audience_fit = subject.get("audience_fit_score")
        if audience_fit is not None and audience_fit < 0.4:
            worsen(Verdict.REWORK_REQUIRED, "baixo fit com a audiência do canal", "audience_fit", "FAIL")

        persistent_days = subject.get("persistent_over_days")
        if persistent_days is not None and persistent_days < 7:
            worsen(
                Verdict.REVIEW_REQUIRED,
                "resultado ainda não sustentado ao longo do tempo (< 7 dias)",
                "persistence", "INSUFFICIENT",
            )

        return GateResult(verdict=verdict, reasons=reasons, criteria=criteria)
