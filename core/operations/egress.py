"""
core.operations.egress — GUARDA CENTRAL de toda saída externa (v1.0.3).

Auditoria de readiness 1.0.2 (bloqueador 1): a trava estava espalhada em
caminhos específicos e três executores reais (busca, análise de página,
OAuth2) além do navegador faziam I/O com o kill switch ENABLED.
Regra agora: TODO ponto de I/O externo do código de produção chama
`egress_blocked()` IMEDIATAMENTE antes do I/O. O teste estático
`tests/test_operacional_1_0_3.py::EgressInventoryTests` enumera todo I/O
externo do código e falha se algum não for precedido por esta guarda.
"""
from __future__ import annotations

from core.operations import kill_switch


def egress_blocked(destination: str | None = None) -> str | None:
    """Motivo do bloqueio (fail-closed) ou None se a saída externa é permitida."""
    reason = kill_switch.block_reason()
    if reason and destination:
        return f"{reason} [destino: {destination}]"
    return reason



# ----------------------------------------------------------------------
# v1.0.4 (auditoria de readiness 1.0.3, bloqueador 2): TRANSPORTE CENTRAL
# obrigatório. A última checagem do kill switch acontece AQUI, no ponto de
# envio — depois de toda a preparação da requisição. O código de produção
# NÃO chama `urllib.request.urlopen` diretamente (teste estático garante).
# ----------------------------------------------------------------------
class EgressBlocked(RuntimeError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def urlopen(request, *, timeout: float):
    import urllib.request

    reason = egress_blocked(getattr(request, "full_url", None) or str(request))
    if reason:
        raise EgressBlocked(reason)
    return urllib.request.urlopen(request, timeout=timeout)
