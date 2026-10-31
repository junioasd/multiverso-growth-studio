"""
atlas.discovery.quality_gate
================================
ATLAS PIPELINE 1 — o pipeline de descoberta integra o MESMO framework
de Quality Gate já existente (`core.quality.gates.QualityGate`/
`GateResult`/`Verdict`) — a mesma base de
`atlas.radar.quality_gate.OpportunityEvidenceGate`/
`core.quality.video_quality.VideoQualityGate` — nenhum framework de
qualidade paralelo foi criado.

Bloqueia (BLOCKED) diante de política incompatível ou risco elevado
comprovado; exige revisão (REVIEW_REQUIRED) diante de evidência
insuficiente ou dado contraditório — nunca aprova por omissão de
evidência.

MICROCORREÇÃO CRÍTICA (defesa em profundidade — falha real encontrada
por auditoria independente): um sinal com `evidence != UNKNOWN` mas
`value` inválido (a string placeholder `"UNKNOWN"`, `None`, vazio, ou
qualquer coisa que não seja um número genuíno) NUNCA passa por este
gate como evidência suficiente — a checagem agora valida o VALOR em
si, independentemente do campo `evidence` declarado pela fonte. Isso
protege contra qualquer fonte (existente ou futura, correta ou
defeituosa) que declare `evidence=REAL` sem um valor genuíno por
trás — o gate nunca confia cegamente no campo `evidence`.

MICROCORREÇÃO CRÍTICA (proveniência — falha real encontrada por
auditoria independente, 2ª rodada): `EvidenceLevel.REAL` sozinho
AINDA não era suficiente — uma fonte podia autodeclarar `REAL` com um
`value` numérico válido, mas sem `source`, sem `claim_type` coerente,
com `confidence=0`, sem `observed_at` válido. Corrigido: o gate agora
TAMBÉM exige `signal.provenance_issues()` vazio (mecanismo explícito
e reutilizável em `atlas.discovery.models.DiscoverySignal`, pensado
para ser satisfeito por futuros adapters reais) — nome de classe da
fonte NUNCA é usado como prova; a proveniência é sempre verificada a
partir dos campos DECLARADOS no próprio sinal.
"""

from __future__ import annotations

import math

from atlas.discovery.models import ContentOpportunity
from atlas.radar.models import EvidenceLevel
from core.quality.gates import GateResult, QualityGate, Verdict

__all__ = ["ContentOpportunityEvidenceGate"]


def _is_valid_evidence_value(value) -> bool:
    """Um valor só é evidência genuína se for um NÚMERO real,
    FINITO — nunca a string placeholder `"UNKNOWN"`, `None`, vazio,
    qualquer outra string, nem `NaN`/`Infinity` (que passam
    `isinstance(x, float)` em Python mas nunca representam uma
    medição genuína — achado de teste adversarial próprio). Aplicado
    INDEPENDENTEMENTE do campo `evidence` declarado — um
    `DiscoverySignal` com `evidence=REAL` mas `value="UNKNOWN"` (ou
    `NaN`/`inf`) NUNCA passa por aqui."""
    if value is None:
        return False
    if isinstance(value, bool):
        return False  # bool é subclasse de int em Python — nunca aceito como sinal numérico
    if isinstance(value, (int, float)):
        return math.isfinite(value)
    return False  # strings (incluindo "UNKNOWN") e qualquer outro tipo nunca são evidência válida


class ContentOpportunityEvidenceGate(QualityGate):
    name = "content_opportunity_evidence_gate"

    _ESSENTIAL_SIGNALS = ("trend_strength", "audience", "competition")

    def evaluate(self, subject: dict) -> GateResult:
        opportunity: ContentOpportunity = subject["opportunity"]
        policy_compatible = subject.get("policy_compatible")
        risk_level = subject.get("risk_level")
        contradictory_data = subject.get("contradictory_data", False)
        policy = subject.get("policy")
        durable_records = subject.get("durable_records") or {}

        reasons: list[str] = []
        criteria: dict[str, str] = {}
        verdict = Verdict.APPROVED

        order = [
            Verdict.BLOCKED, Verdict.REJECTED, Verdict.REWORK_REQUIRED,
            Verdict.REVIEW_REQUIRED, Verdict.UNKNOWN, Verdict.APPROVED,
        ]

        def worsen(new_verdict: Verdict, reason: str, key: str, status: str) -> None:
            nonlocal verdict
            reasons.append(reason)
            criteria[key] = status
            if order.index(new_verdict) < order.index(verdict):
                verdict = new_verdict

        if policy_compatible is False:
            worsen(Verdict.BLOCKED, "política incompatível com a oportunidade", "policy", "FAIL")
        elif policy_compatible is None:
            criteria["policy"] = "NOT_APPLICABLE"  # descoberta é leitura pública — política raramente se aplica
        else:
            criteria["policy"] = "OK"

        if risk_level == "HIGH":
            worsen(Verdict.BLOCKED, "risco elevado identificado na oportunidade", "risk", "FAIL")
        elif risk_level is None:
            criteria["risk"] = "NOT_ASSESSED"
        else:
            criteria["risk"] = "OK"

        if contradictory_data:
            worsen(Verdict.REVIEW_REQUIRED, "dado contraditório encontrado na oportunidade", "data_consistency", "FAIL")
        else:
            criteria["data_consistency"] = "OK"

        for key in self._ESSENTIAL_SIGNALS:
            signal = opportunity.signal(key)
            durable_record = durable_records.get(signal.lineage.acquisition_id) if signal.lineage is not None else None
            trust_issues = signal.trust_issues(key=key, policy=policy, durable_record=durable_record)
            if signal.evidence == EvidenceLevel.UNKNOWN or not _is_valid_evidence_value(signal.value) or trust_issues:
                # MICROCORREÇÃO CRÍTICA (falha real encontrada por
                # auditoria independente — 2ª rodada): `evidence !=
                # UNKNOWN` sozinho NUNCA foi suficiente — uma fonte
                # podia autodeclarar `REAL` sem fonte, sem
                # `claim_type` coerente, sem confiança > 0, sem data
                # de observação válida, e ainda assim o sinal passava.
                #
                # MICROCORREÇÃO ARQUITETURAL CRÍTICA (falha real
                # encontrada por auditoria independente, 5ª rodada —
                # fechamento): mesmo com proveniência sintática
                # completa, uma aquisição genuína podia acontecer e o
                # adapter ainda inventar um valor sem relação alguma
                # com o que foi observado.
                #
                # MICROCORREÇÃO ARQUITETURAL CRÍTICA (falha real
                # encontrada por auditoria independente, 6ª rodada —
                # fechamento): mesmo com lineage íntegra, o valor podia
                # estar fora do domínio semântico da métrica
                # (`trend_strength=50.0`), a lineage podia sobreviver a
                # um restart em modo `TEST` mesmo sem
                # `allow_test_sources`, e a lineage não era ancorada a
                # nenhum registro durável independente — um
                # `acquisition_id` trocado não era detectado. Corrigido:
                # `signal.trust_issues(key=key, policy=policy,
                # durable_record=durable_record)` (a validação CANÔNICA
                # ÚNICA — ver `atlas.discovery.models.DiscoverySignal`,
                # reutilizada também pelo ranking e por qualquer
                # revalidação pós-restart) precisa vir VAZIO — nenhum
                # problema de proveniência, domínio, lineage OU âncora
                # durável. Ausência de qualquer uma dessas provas nunca
                # aprova por omissão; as razões específicas são
                # reportadas explicitamente.
                reason_suffix = f" [confiança: {', '.join(trust_issues)}]" if trust_issues else ""
                worsen(
                    Verdict.REVIEW_REQUIRED,
                    f"sinal essencial '{key}' sem evidência suficiente "
                    f"(evidence={signal.evidence.value}, value={signal.value!r}){reason_suffix}",
                    key, "UNKNOWN",
                )
            else:
                criteria[key] = signal.evidence.value

        return GateResult(verdict=verdict, reasons=reasons, criteria=criteria)
