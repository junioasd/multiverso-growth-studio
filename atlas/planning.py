"""
atlas.planning
================
ATLAS — item 3: "Receber objetivos definidos pelo Owner e transformá-los
em planos operacionais."

`ObjectivePlanner` é deliberadamente simples e determinístico: não
inventa passos "criativos" nem estima resultados — apenas organiza o
objetivo do Owner num checklist operacional alinhado ao pipeline já
documentado do Growth Studio (Production Engine, Seção 8; MultiGerenciador
Comercial, Seção 29). Nenhum dado de mercado, receita ou performance é
fabricado aqui — isso é responsabilidade do `atlas.performance`, sempre
a partir de dados reais.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field


class InvalidObjective(Exception):
    pass


@dataclass
class OperationalPlan:
    plan_id: str
    objective: str
    channel_id: str
    steps: list[str]
    created_at: float = field(default_factory=time.time)


class ObjectivePlanner:
    # Checklist padrão — reflete o fluxo já documentado do Growth Studio
    # (pesquisa -> oportunidade -> funil -> conteúdo -> Quality Gate ->
    # distribuição -> tracking -> revisão -> aprendizado). Não é uma
    # promessa de resultado, apenas a sequência operacional.
    STANDARD_STEPS = (
        "Pesquisar mercado e audiência do canal",
        "Avaliar oportunidades de produto/oferta (Affiliate Marketplace "
        "Intelligence + Product Scoring)",
        "Selecionar funil adequado ao canal e ao público",
        "Planejar conteúdo e CTA alinhados à oferta escolhida",
        "Produzir conteúdo via Production Engine, respeitando os Quality Gates",
        "Distribuir organicamente nos canais autorizados pelo Owner",
        "Rastrear leads, vendas e receita — sempre separando REAL/ESTIMATED/"
        "FORECAST/UNKNOWN",
        "Revisar indicadores reais antes de qualquer decisão de escala",
        "Registrar aprendizado no Learning Loop",
    )

    def build_plan(self, *, objective: str, channel_id: str) -> OperationalPlan:
        if not objective or not objective.strip():
            raise InvalidObjective(
                "objetivo vazio — ATLAS não pode planejar sem um objetivo definido pelo Owner"
            )
        if not channel_id or not channel_id.strip() or channel_id.strip().upper() == "UNKNOWN":
            raise InvalidObjective("channel_id ausente/UNKNOWN — plano precisa de um canal definido")

        return OperationalPlan(
            plan_id=f"plan_{uuid.uuid4().hex[:12]}",
            objective=objective.strip(),
            channel_id=channel_id.strip(),
            steps=list(self.STANDARD_STEPS),
        )
