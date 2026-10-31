"""
radar_web.quality_gate
==========================
SENTINEL — Quality Gate dedicado ao RADAR WEB COMERCIAL.

Reaproveita o MESMO framework genérico já hardened
(`core.quality.gates.QualityGate`/`GateResult`/`Verdict`) — nenhuma
estratégia comercial chega a `VALIDATED`/`APPROVED_KNOWLEDGE`/
`ADOPTED` sem passar por este gate.

CORREÇÃO (item 3 — evidência precisa existir e ser validada): antes,
`bool(strategy.evidence_ids)` bastava — uma string arbitrária como
`"ev-nonexistent"` contava como evidência válida. Agora, CADA
`evidence_id` é RESOLVIDO contra um `core.evidence.registry.
EvidenceRegistry` real — só conta como evidência um ID que resolve
para um `EvidenceRecord` com `verification_status=VERIFIED` (nunca
`MISSING`/`UNVERIFIED`/`INVALID`/`EXPIRED`).

CORREÇÃO (item 4 — métricas: provenance POR métrica): uma métrica
comercial protegida (`core.evidence.claims.NEVER_FABRICATED_METRICS`)
só pode ser tratada como `FACT` quando o valor é um
`core.evidence.metric_evidence.MetricEvidence` cujos `evidence_ids`
PRÓPRIOS resolvem para `VERIFIED` — um número solto (`int`/`float`)
NUNCA é aceito como FACT, mesmo que a estratégia tenha evidência
genérica.

CORREÇÃO (item 8, preservada): `claim_type=FACT` aumenta a exigência
de evidência, nunca a substitui.
"""

from __future__ import annotations

from core.evidence.claims import ClaimType, requires_verifiable_source
from core.evidence.metric_evidence import MetricEvidence
from core.evidence.metric_evidence_validator import MetricEvidenceValidator
from core.evidence.registry import EvidenceRegistry, VerificationStatus
from core.quality.gates import GateResult, QualityGate, Verdict
from radar_web.models import FunnelStrategy

_MIN_CONFIDENCE_FOR_FACT = 0.7


class RadarWebEvidenceGate(QualityGate):
    name = "radar_web_evidence_gate"

    def __init__(self, *, evidence_registry: EvidenceRegistry | None = None) -> None:
        # CORREÇÃO (item 3): sem registry fornecido, usa um registry
        # VAZIO — o padrão mais seguro possível: nenhum evidence_id
        # resolve, tudo vira MISSING até ser genuinamente registrado.
        self.evidence_registry = evidence_registry if evidence_registry is not None else EvidenceRegistry()
        # CORREÇÃO (item 11): validador canônico ÚNICO — reutilizado
        # pelo pipeline de adoção (mesma instância de evidence_registry).
        self._metric_validator = MetricEvidenceValidator(evidence_registry=self.evidence_registry)

    def _resolve_strategy_evidence(self, strategy: FunnelStrategy) -> tuple:
        """Devolve (has_verified_evidence, detalhe_por_id, binding_errors)
        — CORREÇÃO item 3: cada ID é genuinamente resolvido E
        verificado quanto ao vínculo com ESTA estratégia (nunca aceita
        silenciosamente evidência de outra estratégia)."""
        if not strategy.evidence_ids:
            return False, [], []
        details = []
        binding_errors = []
        for eid in strategy.evidence_ids:
            status, binding_error = self.evidence_registry.resolve_status_for(eid, strategy_id=strategy.strategy_id)
            details.append((eid, status.value))
            if binding_error:
                binding_errors.append(binding_error)
        has_verified = any(status == VerificationStatus.VERIFIED.value for _eid, status in details)
        return has_verified, details, binding_errors

    def evaluate(self, subject: dict, *, allow_downgrade: bool = False) -> GateResult:
        """`subject` = {"strategy": FunnelStrategy, "metrics": dict|None}.
        `metrics` mapeia nome->valor — para métricas PROTEGIDAS, o
        valor PRECISA ser um `MetricEvidence` (nunca um número solto)
        para ter QUALQUER chance de ser tratado como FACT.

        CORREÇÃO (item 7 — SENTINEL: bloqueio de claim não comprovado):
        quando `allow_downgrade=True` e a barra de FACT não é
        atingida, `strategy.claim_type` é REBAIXADO in-place para
        `ClaimType.ESTIMATE` (há alguma observação, só não sustentada o
        suficiente para FACT) ou `ClaimType.UNKNOWN` (sem nenhuma
        evidência sequer) — nunca mantém `FACT` sem prova. O
        downgrade fica registrado em `criteria['claim_downgraded_to']`."""
        strategy: FunnelStrategy = subject["strategy"]
        metrics: dict = subject.get("metrics") or {}
        reasons: list = []
        criteria: dict = {}
        missing_evidence: list = []
        invalid_evidence: list = []
        unsupported_claims: list = []
        metric_issues: list = []
        verified_claims: list = []
        unverified_claims: list = []
        provenance_issues: list = []

        # source/provenance
        has_source = bool(strategy.source) and strategy.source not in ("", "UNKNOWN")
        criteria["source"] = "PASS" if has_source else "FAIL"
        if not has_source:
            reasons.append("estratégia sem `source` identificável")
            missing_evidence.append("source")
            provenance_issues.append("source ausente/inválida")

        # CORREÇÃO (item 3): evidência REALMENTE resolvida + binding
        # com a estratégia verificado
        has_evidence, evidence_details, strategy_binding_errors = self._resolve_strategy_evidence(strategy)
        criteria["evidence"] = "PASS" if has_evidence else "MISSING"
        if not strategy.evidence_ids:
            missing_evidence.append("evidence_ids")
            reasons.append("nenhum evidence_id associado à estratégia")
        elif not has_evidence:
            missing_evidence.append(f"evidence_ids sem resolução VERIFIED: {evidence_details}")
            reasons.append(f"evidence_ids declarados não resolvem para evidência VERIFIED no registry: {evidence_details}")
            for eid, status in evidence_details:
                if status == VerificationStatus.MISSING.value:
                    unsupported_claims.append(f"evidence_id '{eid}' não existe no Evidence Registry")
                elif status in (VerificationStatus.INVALID.value, VerificationStatus.EXPIRED.value):
                    # CORREÇÃO (item 7): INVALID/EXPIRED é distinto de
                    # MISSING — o ID EXISTE, mas não pode sustentar a
                    # estratégia.
                    invalid_evidence.append(f"evidence_id '{eid}' está {status}")
        if strategy_binding_errors:
            reasons.extend(strategy_binding_errors)
            unsupported_claims.extend(strategy_binding_errors)

        # freshness
        stale = strategy.is_stale
        criteria["freshness"] = "STALE" if stale else "FRESH"
        if stale:
            reasons.append("conhecimento obsoleto (stale) — freshness insuficiente")
            missing_evidence.append("freshness")

        # confidence
        criteria["confidence"] = f"{strategy.confidence:.2f}"
        low_confidence = strategy.confidence < 0.5

        # claim_type declarado
        criteria["claim_type"] = strategy.claim_type.value
        undeclared_claim = strategy.claim_type == ClaimType.UNKNOWN
        if undeclared_claim:
            unsupported_claims.append("claim_type=UNKNOWN — nenhuma classificação declarada")

        # FACT aumenta a exigência — nunca a substitui (item 8 rodada anterior)
        fact_bar_met = True
        if strategy.claim_type == ClaimType.FACT:
            # CORREÇÃO (itens 7/9 desta rodada): FACT comercial exige
            # `primary_claim_id` resolvendo de verdade no ClaimRegistry
            # E pertencendo à MESMA estratégia sendo avaliada — nunca
            # confia apenas em `claim_type` solto.
            claim_record = None
            claim_ok = False
            if not strategy.primary_claim_id:
                reasons.append("claim_type=FACT exige `primary_claim_id` — nenhuma claim vinculada à estratégia")
                unsupported_claims.append("FACT sem primary_claim_id — CLAIM_REQUIRED")
            else:
                claim_record = self.evidence_registry.claim_registry.resolve(strategy.primary_claim_id)
                if claim_record is None:
                    reasons.append(f"primary_claim_id '{strategy.primary_claim_id}' não resolve no ClaimRegistry")
                    unsupported_claims.append(f"claim '{strategy.primary_claim_id}' não registrada")
                elif claim_record.strategy_id != strategy.strategy_id:
                    reasons.append(
                        f"CLAIM_BINDING_ERROR: claim '{strategy.primary_claim_id}' pertence à estratégia "
                        f"'{claim_record.strategy_id}', não a '{strategy.strategy_id}'"
                    )
                    unsupported_claims.append("CLAIM_BINDING_ERROR")
                else:
                    claim_ok = True

            # CORREÇÃO (itens 13/14 desta rodada): `strategy.source`
            # (a fonte de DESCOBERTA da estratégia) precisa RESOLVER de
            # verdade no SourceRegistry para sustentar FACT — nunca
            # basta ser uma string não vazia.
            strategy_source_ok = self.evidence_registry.source_registry.can_sustain_fact(strategy.source)
            criteria["strategy_source"] = "PASS" if strategy_source_ok else "FAIL"
            if not strategy_source_ok:
                reasons.append(
                    f"strategy.source '{strategy.source}' não resolve/não sustenta FACT no SourceRegistry"
                )
                provenance_issues.append(f"strategy.source '{strategy.source}' não registrada")

            fact_bar_met = (
                claim_ok and strategy_source_ok and has_source and has_evidence and not stale
                and strategy.confidence >= _MIN_CONFIDENCE_FOR_FACT
            )
            criteria["fact_evidence_bar"] = "PASS" if fact_bar_met else "FAIL"
            criteria["claim_binding"] = "PASS" if claim_ok else "FAIL"
            if fact_bar_met:
                verified_claims.append("estratégia: claim_type=FACT sustentado por evidência VERIFIED + claim vinculada")
            else:
                if claim_ok:
                    reasons.append(
                        "claim_type=FACT exige barra de evidência mais alta (source + evidence VERIFIED + "
                        f"freshness + confidence>={_MIN_CONFIDENCE_FOR_FACT}) — não presente"
                    )
                unsupported_claims.append("claim_type=FACT sem evidência/claim VERIFIED suficiente")
                unverified_claims.append("estratégia: claim_type=FACT rebaixável a ESTIMATE/INFERENCE/UNKNOWN")

        # CORREÇÃO (item 4 rodada anterior + itens 1/2 desta rodada):
        # métricas protegidas exigem MetricEvidence PRÓPRIA, VALIDADA
        # (resolve + existe + status + binding com a métrica + binding
        # com a estratégia) — nunca um número solto, nunca "emprestada"
        # CORREÇÃO (item 11 — MetricEvidenceValidator centralizado):
        # todo o processamento de métrica passa pelo MESMO validador
        # canônico usado também por `record_metrics()`/`validate()` no
        # pipeline de adoção — nunca lógicas divergentes em pontos
        # diferentes.
        fabricated_metrics = []
        for key, value in metrics.items():
            if not requires_verifiable_source(key):
                continue
            outcome = self._metric_validator.validate(requested_key=key, metric_evidence=value, strategy_id=strategy.strategy_id)
            if not outcome.is_valid:
                fabricated_metrics.append(key)
                metric_issues.extend(outcome.issues)
                unverified_claims.append(f"métrica '{key}' ({outcome.status.value})")
            else:
                verified_claims.append(f"métrica '{key}' (MetricEvidence VERIFIED, binding correto, ProvenanceRecord VERIFIED)")

        if fabricated_metrics:
            criteria["metric_provenance"] = f"UNVERIFIED: {fabricated_metrics}"
            reasons.append(
                f"métrica(s) {fabricated_metrics} sem MetricEvidence vinculada e VERIFIED — "
                "nunca tratadas como FACT"
            )
        else:
            criteria["metric_provenance"] = "PASS"

        # veredito — prioridade de severidade
        if fabricated_metrics or not has_source:
            verdict = Verdict.REJECTED
        elif not has_evidence or undeclared_claim or not fact_bar_met:
            verdict = Verdict.BLOCKED
        elif stale:
            verdict = Verdict.REWORK_REQUIRED
        elif low_confidence:
            verdict = Verdict.REVIEW_REQUIRED
        else:
            verdict = Verdict.APPROVED

        # CORREÇÃO (item 7): downgrade explícito — FACT nunca
        # permanece sem prova quando `allow_downgrade=True`. Rebaixa
        # para ESTIMATE quando há AO MENOS alguma evidência associada
        # (mesmo insuficiente para FACT), ou UNKNOWN quando não há
        # nenhuma — nunca finge uma classificação intermediária sem
        # base.
        claim_downgraded_to = None
        if allow_downgrade and strategy.claim_type == ClaimType.FACT and not fact_bar_met:
            claim_downgraded_to = ClaimType.ESTIMATE if strategy.evidence_ids else ClaimType.UNKNOWN
            strategy.claim_type = claim_downgraded_to
            criteria["claim_downgraded_to"] = claim_downgraded_to.value

        return GateResult(
            verdict=verdict, reasons=reasons,
            criteria={
                **criteria,
                "missing_evidence": missing_evidence, "invalid_evidence": invalid_evidence,
                "unsupported_claims": unsupported_claims,
                "metric_issues": metric_issues, "verified_claims": verified_claims,
                "unverified_claims": unverified_claims, "stale_evidence": [strategy.strategy_id] if stale else [],
                "provenance_issues": provenance_issues,
            },
        )
