"""
bridge.execution.webhooks
==============================
BLOCO FINAL 2 (item 5 — automação externa: webhooks, retries,
recuperação de falhas). `WebhookDispatcher` — entrega REAL de webhook
via HTTP (`urllib.request`), assinado com HMAC-SHA256 (autenticidade
real, verificável pelo destinatário) — nunca uma "simulação de
entrega". `RetryPolicy` — backoff exponencial real, com limite
máximo de tentativas (nunca infinito).
"""

from __future__ import annotations

from core.operations import egress as _egress_transport

import hashlib
import hmac
import json
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field


@dataclass
class WebhookDeliveryResult:
    success: bool
    delivery_id: str
    status_code: int | None = None
    attempts: int = 0
    reason: str = ""
    delivered_at: float | None = None
    # COMANDO 3/6: EXECUTED | NOT_SENT | TRANSIENT | REJECTED | UNKNOWN
    # (mesma semântica de `platform_adapters.PlatformActionResult.delivery`).
    delivery: str = ""


@dataclass
class RetryPolicy:
    """Backoff exponencial REAL — nunca tentativas infinitas.
    `max_attempts` limita o total; `base_delay_seconds` cresce
    geometricamente a cada tentativa (`base * 2^(tentativa-1)`),
    limitado por `max_delay_seconds`."""

    max_attempts: int = 5
    base_delay_seconds: float = 1.0
    max_delay_seconds: float = 60.0

    def delay_for_attempt(self, attempt_number: int) -> float:
        if attempt_number < 1:
            raise ValueError("attempt_number deve ser >= 1")
        delay = self.base_delay_seconds * (2 ** (attempt_number - 1))
        return min(delay, self.max_delay_seconds)


def sign_payload(*, payload_bytes: bytes, secret: str) -> str:
    """Assinatura HMAC-SHA256 REAL — o destinatário pode verificar
    autenticidade comparando com a mesma função usando o secret
    compartilhado (nunca um hash sem chave, que qualquer um poderia
    forjar)."""
    return hmac.new(secret.encode("utf-8"), payload_bytes, hashlib.sha256).hexdigest()


class WebhookDispatcher:
    def __init__(self, *, retry_policy: RetryPolicy | None = None, timeout_seconds: float = 10.0, sleep_fn=None) -> None:
        self.retry_policy = retry_policy if retry_policy is not None else RetryPolicy()
        self.timeout_seconds = timeout_seconds
        # CORREÇÃO: `sleep_fn` injetável — permite testes determinísticos
        # sem `time.sleep` real (mesma disciplina de suite-order-stability).
        self._sleep_fn = sleep_fn if sleep_fn is not None else time.sleep

    def deliver(self, *, url: str, payload: dict, secret: str | None = None, delivery_id: str | None = None) -> WebhookDeliveryResult:
        from core.operations import kill_switch as _ks   # v1.0.1: KILL SWITCH

        _blocked = _ks.block_reason()
        if _blocked:
            return WebhookDeliveryResult(success=False, delivery_id=delivery_id or "", attempts=0, reason=_blocked,
                                         delivery="NOT_SENT")
        """Entrega REAL com retry real — cada tentativa é uma
        requisição HTTP genuína. Nunca marca sucesso sem uma resposta
        HTTP 2xx real recebida."""
        delivery_id = delivery_id or f"whd_{uuid.uuid4().hex[:12]}"
        body = json.dumps({"delivery_id": delivery_id, "payload": payload}).encode("utf-8")
        headers = {"Content-Type": "application/json", "X-Webhook-Delivery-Id": delivery_id}
        if secret:
            headers["X-Webhook-Signature"] = sign_payload(payload_bytes=body, secret=secret)

        # COMANDO 3/6 (defeito provado com servidor real): antes, QUALQUER
        # falha gerava nova tentativa — um timeout aguardando a resposta
        # (o receptor JÁ processou) fez 1 entrega lógica chegar 3x. Agora
        # só se repete o que é CERTO que não foi processado (falha de
        # conexão/envio, 408/429/503); resultado ambíguo encerra como
        # UNKNOWN e recusa definitiva (4xx) encerra como REJECTED.
        last_reason, last_delivery = "", "NOT_SENT"
        for attempt in range(1, self.retry_policy.max_attempts + 1):
            # v1.0.2 (auditoria de readiness, bloqueador 3): revalida o kill
            # switch IMEDIATAMENTE antes de cada tentativa — retry nunca contorna.
            from core.operations.egress import egress_blocked   # v1.0.3: guarda central, a CADA tentativa

            _blocked_now = egress_blocked(url)
            if _blocked_now:
                return WebhookDeliveryResult(success=False, delivery_id=delivery_id, attempts=attempt - 1,
                                             reason=_blocked_now, delivery=last_delivery if attempt > 1 else "NOT_SENT")
            request = urllib.request.Request(url, data=body, method="POST", headers=headers)
            try:
                with _egress_transport.urlopen(request, timeout=self.timeout_seconds) as response:
                    if 200 <= response.status < 300:
                        return WebhookDeliveryResult(
                            success=True, delivery_id=delivery_id, status_code=response.status, attempts=attempt,
                            delivered_at=time.time(), delivery="EXECUTED",
                        )
                    last_reason, last_delivery = f"resposta HTTP {response.status} (não é 2xx)", "UNKNOWN"
            except _egress_transport.EgressBlocked as _eb:   # v1.0.4: bloqueio NO ponto de envio
                return WebhookDeliveryResult(success=False, delivery_id=delivery_id, attempts=attempt - 1, reason=_eb.reason, delivery=last_delivery if attempt > 1 else "NOT_SENT")
            except urllib.error.HTTPError as exc:
                from bridge.execution.platform_adapters import _classify_http_error

                last_reason, last_delivery = f"HTTP {exc.code}: {exc.reason}", _classify_http_error(exc.code)
            except urllib.error.URLError as exc:
                last_reason, last_delivery = f"falha real de rede: {getattr(exc, 'reason', exc)}", "NOT_SENT"
            except Exception as exc:
                last_reason, last_delivery = f"resultado DESCONHECIDO após o envio: {exc}", "UNKNOWN"

            if last_delivery in ("UNKNOWN", "REJECTED"):
                return WebhookDeliveryResult(
                    success=False, delivery_id=delivery_id, attempts=attempt, delivery=last_delivery,
                    reason=f"{last_reason} — entrega NÃO repetida "
                           + ("(o receptor pode ter processado)" if last_delivery == "UNKNOWN" else "(recusa definitiva)"),
                )
            if attempt < self.retry_policy.max_attempts:
                self._sleep_fn(self.retry_policy.delay_for_attempt(attempt))

        return WebhookDeliveryResult(
            success=False, delivery_id=delivery_id, attempts=self.retry_policy.max_attempts, delivery=last_delivery,
            reason=f"todas as {self.retry_policy.max_attempts} tentativas falharam — última: {last_reason}",
        )

    @staticmethod
    def verify_signature(*, payload_bytes: bytes, secret: str, received_signature: str) -> bool:
        """Verificação REAL de assinatura recebida — usa comparação em
        tempo constante (`hmac.compare_digest`) para evitar timing
        attacks, nunca `==` direto em segredo."""
        expected = sign_payload(payload_bytes=payload_bytes, secret=secret)
        return hmac.compare_digest(expected, received_signature)
