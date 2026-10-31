"""
core.agents.runtime.juris
=============================
BLOCO FINAL 1 (item 1) — JURIS: agente REAL de compliance/copyright.
Checagem baseada em REGRAS explícitas e auditáveis (nunca uma "opinião
jurídica" gerada livremente) — cada veredito aponta exatamente qual
regra foi violada/respeitada. Nunca declara "aprovado" por ausência de
verificação — item não coberto por nenhuma regra conhecida é reportado
como `NEEDS_HUMAN_REVIEW`, nunca `APPROVED` por omissão.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.agents.runtime.contracts import AgentTaskResult, RealAgentContract
from core.capabilities.registry import Capability


@dataclass
class ComplianceRule:
    rule_id: str
    description: str
    check: object  # Callable[[dict], bool] -- True = violação detectada


def _uses_third_party_ip_without_license(payload: dict) -> bool:
    return bool(payload.get("uses_third_party_brand_or_ip")) and not payload.get("has_license")


def _reproduces_copyrighted_material_verbatim(payload: dict) -> bool:
    return bool(payload.get("reproduces_copyrighted_material_verbatim"))


def _claims_permission_without_evidence(payload: dict) -> bool:
    return bool(payload.get("claims_licensed")) and not payload.get("license_evidence_id")


def _uses_real_person_likeness_without_consent(payload: dict) -> bool:
    return bool(payload.get("depicts_real_person")) and not payload.get("has_consent")


# CORREÇÃO (item — auditável, nunca "opinião jurídica" livre): cada
# regra é uma função PURA e determinística sobre o payload — o
# resultado é sempre rastreável a QUAL regra disparou.
_DEFAULT_RULES = (
    ComplianceRule("IP_001", "Uso de marca/propriedade intelectual de terceiros sem licença", _uses_third_party_ip_without_license),
    ComplianceRule("IP_002", "Reprodução verbatim de material protegido por direitos autorais", _reproduces_copyrighted_material_verbatim),
    ComplianceRule("IP_003", "Alegação de licença sem evidência de licença registrada", _claims_permission_without_evidence),
    ComplianceRule("IP_004", "Uso da imagem/semelhança de pessoa real sem consentimento", _uses_real_person_likeness_without_consent),
)


class JurisAgent(RealAgentContract):
    agent_id = "JURIS"
    primary_task_types = frozenset({"CHECK_COMPLIANCE", "LIST_RULES"})
    all_rounder_task_types = frozenset({"CHECK_COMPLIANCE"})
    max_input_items = 50

    def __init__(self, *, rules: tuple = _DEFAULT_RULES, audit=None) -> None:
        super().__init__(audit=audit)
        self.rules = rules

    def execute_real(self, *, task_type: str, payload: dict) -> AgentTaskResult:
        if task_type == "CHECK_COMPLIANCE":
            return self._check_compliance(payload)
        if task_type == "LIST_RULES":
            return AgentTaskResult.completed(
                agent_id=self.agent_id, task_type="LIST_RULES",
                output={"rules": [{"rule_id": r.rule_id, "description": r.description} for r in self.rules]},
                confidence=1.0,
            )
        return AgentTaskResult.rejected(agent_id=self.agent_id, task_type=task_type, reason="task_type não implementado")

    def _check_compliance(self, payload: dict) -> AgentTaskResult:
        item = payload.get("item")
        if not isinstance(item, dict):
            return AgentTaskResult.rejected(
                agent_id=self.agent_id, task_type="CHECK_COMPLIANCE",
                reason="'item' (dict com os atributos a checar) é obrigatório",
            )
        violations = []
        for rule in self.rules:
            try:
                if rule.check(item):
                    violations.append({"rule_id": rule.rule_id, "description": rule.description})
            except Exception as exc:
                # uma regra que falha ao avaliar NUNCA é tratada como "aprovado" --
                # vira pendência explícita de revisão humana.
                return AgentTaskResult.completed(
                    agent_id=self.agent_id, task_type="CHECK_COMPLIANCE",
                    output={"verdict": "NEEDS_HUMAN_REVIEW", "reason": f"regra '{rule.rule_id}' falhou ao avaliar: {exc}"},
                    confidence=0.0,
                )

        if violations:
            verdict = "BLOCKED"
            confidence = 1.0
        else:
            # CORREÇÃO: ausência de violação conhecida NUNCA vira "APPROVED"
            # automático -- só os campos genuinamente cobertos pelas regras
            # atuais foram checados; itens fora da cobertura das regras
            # continuam precisando de revisão humana antes de publicar.
            verdict = "NO_KNOWN_VIOLATION"
            confidence = 1.0 if item else 0.0

        return AgentTaskResult.completed(
            agent_id=self.agent_id, task_type="CHECK_COMPLIANCE",
            output={"verdict": verdict, "violations": violations, "rules_checked": len(self.rules)},
            evidence_ids=[v["rule_id"] for v in violations], confidence=confidence,
        )


def register_juris_capabilities(capability_registry, *, granted_by: str) -> None:
    from core.capabilities.registry import CapabilityHealth, CompetencyLevel

    capability_registry.register(
        agent="JURIS", capability=Capability.QUALITY_CONTROL, level=CompetencyLevel.ESPECIALISTA,
        granted_by=granted_by, provider="internal", available=True, authorized=True,
        health=CapabilityHealth.HEALTHY,
    )
