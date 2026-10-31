"""
core.agents.runtime.scriptor
================================
BLOCO FINAL 1 (item 1) — SCRIPTOR: agente REAL de roteiros/copy.
Nunca "gera" texto livremente (isso exigiria um modelo generativo,
fora do alcance deste ambiente) — em vez disso, faz montagem REAL e
determinística de templates estruturados a partir de slots fornecidos
pelo chamador (hook/corpo/CTA, por exemplo). Template desconhecido ou
slot obrigatório ausente é sempre um erro explícito — nunca
preenchido com um placeholder genérico apresentado como copy real.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.agents.runtime.contracts import AgentTaskResult, RealAgentContract
from core.capabilities.registry import Capability


@dataclass
class ScriptTemplate:
    template_id: str
    structure: str  # string com {slot_name} — mesma sintaxe de str.format
    required_slots: list = field(default_factory=list)


_DEFAULT_TEMPLATES = {
    "SHORT_HOOK_BODY_CTA": ScriptTemplate(
        template_id="SHORT_HOOK_BODY_CTA", structure="{hook}\n\n{body}\n\n{cta}",
        required_slots=["hook", "body", "cta"],
    ),
    "LONGFORM_INTRO_SECTIONS_OUTRO": ScriptTemplate(
        template_id="LONGFORM_INTRO_SECTIONS_OUTRO", structure="{intro}\n\n{sections}\n\n{outro}",
        required_slots=["intro", "sections", "outro"],
    ),
}


class ScriptorAgent(RealAgentContract):
    agent_id = "SCRIPTOR"
    primary_task_types = frozenset({"ASSEMBLE_SCRIPT", "LIST_TEMPLATES"})
    all_rounder_task_types = frozenset({"LIST_TEMPLATES"})
    max_input_items = 50

    def __init__(self, *, templates: dict | None = None, audit=None) -> None:
        super().__init__(audit=audit)
        self.templates = templates if templates is not None else dict(_DEFAULT_TEMPLATES)

    def execute_real(self, *, task_type: str, payload: dict) -> AgentTaskResult:
        if task_type == "ASSEMBLE_SCRIPT":
            return self._assemble_script(payload)
        if task_type == "LIST_TEMPLATES":
            return AgentTaskResult.completed(
                agent_id=self.agent_id, task_type="LIST_TEMPLATES",
                output={"templates": [
                    {"template_id": t.template_id, "required_slots": t.required_slots} for t in self.templates.values()
                ]},
                confidence=1.0,
            )
        return AgentTaskResult.rejected(agent_id=self.agent_id, task_type=task_type, reason="task_type não implementado")

    def _assemble_script(self, payload: dict) -> AgentTaskResult:
        template_id = payload.get("template_id")
        slots = payload.get("slots")
        if not template_id or not isinstance(slots, dict):
            return AgentTaskResult.rejected(
                agent_id=self.agent_id, task_type="ASSEMBLE_SCRIPT",
                reason="template_id e slots (dict) são obrigatórios",
            )
        template = self.templates.get(template_id)
        if template is None:
            return AgentTaskResult.rejected(
                agent_id=self.agent_id, task_type="ASSEMBLE_SCRIPT",
                reason=f"template_id '{template_id}' desconhecido — nunca inventa um template",
            )
        missing = [s for s in template.required_slots if s not in slots or not slots[s]]
        if missing:
            return AgentTaskResult.rejected(
                agent_id=self.agent_id, task_type="ASSEMBLE_SCRIPT",
                reason=f"slots obrigatórios ausentes para '{template_id}': {missing}",
            )
        assembled = template.structure.format(**{k: slots[k] for k in template.required_slots})
        return AgentTaskResult.completed(
            agent_id=self.agent_id, task_type="ASSEMBLE_SCRIPT",
            output={"script": assembled, "template_id": template_id, "slots_used": template.required_slots},
            confidence=1.0,
        )


def register_scriptor_capabilities(capability_registry, *, granted_by: str) -> None:
    from core.capabilities.registry import CapabilityHealth, CompetencyLevel

    capability_registry.register(
        agent="SCRIPTOR", capability=Capability.MULTIMODAL_TEXT, level=CompetencyLevel.ESPECIALISTA,
        granted_by=granted_by, provider="internal", available=True, authorized=True,
        health=CapabilityHealth.HEALTHY,
    )
