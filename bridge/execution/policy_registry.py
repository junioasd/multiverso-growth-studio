"""
bridge.execution.policy_registry
=====================================
MICROCORREÇÃO BF2 (item 1 — CRITICALIDADE NÃO PODE SER CONTROLADA
PELO CHAMADOR). `PolicyActionRegistry` — fonte ÚNICA e AUTORITATIVA
da criticidade real de uma ação. O parâmetro `criticality` que o
CHAMADOR passa para `NavigatorReal.execute_action()` NUNCA pode
rebaixar uma ação que este registro classifica como `CRITICAL` — na
pior hipótese, o chamador pode ESCALAR uma ação (pedir CRITICAL para
algo que o registro trataria como ROUTINE), nunca o contrário.

Classificação:
1. Ações EXPLICITAMENTE registradas como leitura pura (lista abaixo,
   nomes exatos conhecidos das APIs reais já integradas) → `ROUTINE`.
2. Qualquer ação cujo nome contenha uma palavra-chave de efeito
   externo (escrita/publicação/deleção/gasto/alteração — `UPDATE`,
   `PUBLISH`, `DELETE`, `SPEND`, `CREATE`, `POST`, `SET`, `REMOVE`,
   `MODIFY`, `WRITE`, `SEND`, `TRANSFER`, `PAY`, `CANCEL`, `REVOKE`,
   `PATCH`, `PUT`, `UPLOAD`, `INCREASE`, `APPROVE`, `REJECT`) →
   `CRITICAL`, mesmo que o nome também comece com um prefixo de
   leitura (ex: "GET_AND_DELETE" seria CRITICAL — a palavra-chave
   crítica sempre vence).
3. Ação DESCONHECIDA (não bate com nenhuma das duas regras acima) →
   `CRITICAL` por padrão (deny-by-default) — nunca assume que uma
   ação nova/não catalogada é segura.
"""

from __future__ import annotations

from enum import Enum


class ActionCriticality(str, Enum):
    ROUTINE = "ROUTINE"    # leitura/consulta -- nunca precisa de aprovação
    CRITICAL = "CRITICAL"  # publica, gasta, deleta, ou qualquer ação irreversível/externa visível


# CORREÇÃO (item 1): lista FECHADA de ações conhecidas, genuinamente
# somente-leitura, das APIs reais já integradas (bridge/execution/
# platforms.py e commercial_platforms.py) — nunca um prefixo genérico
# sozinho decide ROUTINE; cada nome exato foi revisado.
_KNOWN_ROUTINE_ACTIONS = frozenset({
    "GET_CHANNEL_INFO", "LIST_VIDEOS", "GET_VIDEO_STATISTICS",           # YouTube
    "GET_PAGE_INFO", "GET_POST_INSIGHTS", "GET_INSTAGRAM_MEDIA_INFO",    # Meta
    "GET_USER_INFO", "GET_VIDEO_ANALYTICS",                             # TikTok
    "LIST_SALES", "GET_SUBSCRIPTION_STATUS", "GET_COMMISSIONS",         # Hotmart
    "GET_SALE_DETAILS",                                                 # Monetizze
    "RUN_REPORT",                                                       # Google Analytics
    "GET_CAMPAIGN_PERFORMANCE",                                         # Google Ads (somente leitura)
    "GET_CHANNEL_INFO", "SCAN_MARKET_OPPORTUNITIES", "RESEARCH_AUDIENCE",
    "GET_INFO",
})

_CRITICAL_KEYWORDS = (
    "UPDATE", "PUBLISH", "DELETE", "SPEND", "CREATE", "POST", "SET", "REMOVE", "MODIFY", "WRITE",
    "SEND", "TRANSFER", "PAY", "CANCEL", "REVOKE", "PATCH", "PUT", "UPLOAD", "INCREASE", "APPROVE",
    "REJECT", "GRANT", "REGISTER", "SUBSCRIBE", "UNSUBSCRIBE", "BAN", "BLOCK", "SUSPEND",
)

_ROUTINE_PREFIXES = ("GET_", "LIST_", "QUERY_", "FETCH_", "READ_", "VIEW_", "CHECK_", "SCAN_", "RESEARCH_")


class PolicyActionRegistry:
    """Autoridade única sobre criticidade de ação. Sem estado mutável
    (classificação é sempre uma função pura do nome da ação) — nunca
    pode ser "reconfigurada" silenciosamente em tempo de execução por
    um chamador não-autorizado."""

    def resolve_required_criticality(self, *, action: str) -> ActionCriticality:
        action_upper = (action or "").upper()

        # regra 2 primeiro -- palavra-chave crítica SEMPRE vence, mesmo
        # que o nome também pareça começar com um prefixo de leitura.
        if any(keyword in action_upper for keyword in _CRITICAL_KEYWORDS):
            return ActionCriticality.CRITICAL

        if action_upper in _KNOWN_ROUTINE_ACTIONS:
            return ActionCriticality.ROUTINE

        if any(action_upper.startswith(prefix) for prefix in _ROUTINE_PREFIXES):
            return ActionCriticality.ROUTINE

        # regra 3 -- deny-by-default: ação desconhecida nunca é assumida
        # segura.
        return ActionCriticality.CRITICAL

    def effective_criticality(self, *, action: str, caller_requested: ActionCriticality) -> ActionCriticality:
        """CORREÇÃO (item 1 — núcleo da correção): combina a
        classificação AUTORITATIVA com o que o chamador pediu — o
        chamador só pode ESCALAR (pedir CRITICAL para algo que o
        registro trataria como ROUTINE), NUNCA rebaixar uma ação que
        o registro classifica como CRITICAL."""
        required = self.resolve_required_criticality(action=action)
        if required == ActionCriticality.CRITICAL:
            return ActionCriticality.CRITICAL
        if caller_requested == ActionCriticality.CRITICAL:
            return ActionCriticality.CRITICAL
        return ActionCriticality.ROUTINE
