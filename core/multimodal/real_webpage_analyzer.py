"""
core.multimodal.real_webpage_analyzer
==========================================
MICROCORREÇÃO — item 3 (MULTIMODAL: página web) + item 2 (base para
INTELIGÊNCIA WEB/MERCADO). Busca REAL de página via
`urllib.request` (stdlib — sem dependência de rede paga) — nunca
simula uma resposta. Neste ambiente, sem acesso à rede configurado
para o processo Python, toda chamada real falha com um erro de rede
genuíno (`URLError`/timeout) — capturado e reportado como
`UNAVAILABLE` explicitamente, com o motivo REAL do erro, nunca uma
página fabricada. Em um ambiente com rede disponível, este MESMO
código faz uma requisição HTTP real e extrai texto real via
`html.parser` (stdlib).
"""

from __future__ import annotations

from core.operations import egress as _egress_transport

import hashlib
import time
import urllib.error
import urllib.request
from html.parser import HTMLParser

from core.evidence.claims import ClaimType
from core.multimodal.evidence import Modality, MultimodalEvidence

# MICROCORREÇÃO (suite-order stability): reduzido de 8.0s para 3.0s —
# nunca aumentar timeout para mascarar problema; aqui reduz o pior
# caso de qualquer tentativa de rede real (bem-sucedida ou não).
_DEFAULT_TIMEOUT_SECONDS = 3.0
_MAX_CONTENT_BYTES = 2_000_000


class _TextExtractingParser(HTMLParser):
    """Extração REAL de texto visível de HTML — stdlib puro, nunca um
    parser fabricado. Ignora conteúdo de <script>/<style>."""

    def __init__(self):
        super().__init__()
        self._skip_stack = []
        self.text_parts: list = []
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip_stack.append(tag)
        if tag == "title":
            self._in_title = True

    def handle_endtag(self, tag):
        if self._skip_stack and self._skip_stack[-1] == tag:
            self._skip_stack.pop()
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._skip_stack:
            return
        if self._in_title:
            self.title += data
        stripped = data.strip()
        if stripped:
            self.text_parts.append(stripped)


class RealWebPageAnalyzer:
    """Busca e extração REAL de página web — `reference` é sempre uma
    URL http(s). Sem rede disponível (o caso deste ambiente), a
    tentativa de conexão real falha e o motivo genuíno do erro é
    reportado — nunca uma página fabricada."""

    modality = Modality.WEB_PAGE

    def analyze(self, *, reference: str, context: dict | None = None) -> MultimodalEvidence:
        from core.operations.egress import egress_blocked   # v1.0.3: guarda central de saída

        _blocked = egress_blocked(reference)
        if _blocked:
            return MultimodalEvidence.unavailable(modality=self.modality, reason=_blocked)
        if not reference or not reference.lower().startswith(("http://", "https://")):
            return MultimodalEvidence.unavailable(
                modality=self.modality, reason=f"reference precisa ser uma URL http(s) válida — recebido: '{reference}'",
            )
        timeout = (context or {}).get("timeout_seconds", _DEFAULT_TIMEOUT_SECONDS)
        request = urllib.request.Request(reference, headers={"User-Agent": "MultiversoGrowthStudio/1.0"})
        fetch_start = time.time()
        try:
            with _egress_transport.urlopen(request, timeout=timeout) as response:
                raw = response.read(_MAX_CONTENT_BYTES)
                status_code = response.status
                content_type = response.headers.get("Content-Type", "")
        except _egress_transport.EgressBlocked as _eb:   # v1.0.4: bloqueio NO ponto de envio
            return MultimodalEvidence.unavailable(modality=self.modality, reason=_eb.reason)
        except urllib.error.URLError as exc:
            return MultimodalEvidence.unavailable(
                modality=self.modality,
                reason=f"falha real de rede ao buscar '{reference}': {exc.reason if hasattr(exc, 'reason') else exc}",
            )
        except Exception as exc:
            return MultimodalEvidence.unavailable(
                modality=self.modality, reason=f"falha ao buscar/processar página real: {exc}",
            )
        fetch_duration = time.time() - fetch_start

        try:
            html_text = raw.decode("utf-8", errors="replace")
            parser = _TextExtractingParser()
            parser.feed(html_text)
            visible_text = " ".join(parser.text_parts)
        except Exception as exc:
            return MultimodalEvidence.unavailable(
                modality=self.modality, reason=f"página buscada mas falha ao extrair texto real: {exc}",
            )

        summary = (
            f"HTTP {status_code}, {len(raw)} bytes, título='{parser.title.strip()[:80]}', "
            f"{len(visible_text)} caracteres de texto visível extraídos em {fetch_duration:.2f}s"
        )
        return MultimodalEvidence.new(
            modality=self.modality, source=reference,
            provenance=f"busca HTTP real via urllib.request (stdlib), buscado em {time.time():.0f}",
            confidence=1.0, claim_type=ClaimType.FACT, summary=summary,
            reference_hash=hashlib.sha256(raw).hexdigest(),
            content_text=(parser.title.strip() + "\n" + visible_text)[:_MAX_CONTENT_BYTES],
        )
