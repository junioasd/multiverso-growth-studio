"""
bridge.execution.browser_executor
======================================
BLOCO FINAL 2 (item 1/5 — navegador automatizado controlado como
fallback / browser automation) + MICROCORREÇÃO (item 4 — detectar
ausência do binário do Playwright corretamente). `BrowserExecutor` —
automação REAL de navegador via Playwright. Usado como MECANISMO DE
FALLBACK quando nenhuma API oficial/conector está disponível para uma
plataforma — nunca a primeira escolha (a ordem de prioridade real
continua sendo API oficial > conector/MCP > navegador > Computer
Use/RPA, já implementada em `bridge.hub.EXECUTION_MECHANISM_PRIORITY`).

A biblioteca Playwright (`pip install playwright`) NÃO inclui os
binários de navegador — eles são baixados separadamente via
`playwright install chromium` (ver `requirements.txt` para o passo
completo de instalação). Sem esse passo, `is_browser_available()`
detecta a ausência do binário de forma explícita e rápida (antes de
sequer tentar lançar), e todo método público retorna
`BrowserActionResult(success=False)` com o motivo claro — nunca uma
exceção não tratada, nunca finge que a automação funcionou sem
verificação real.

Nunca finge sucesso: se o navegador não conseguir navegar/executar a
ação real (rede indisponível, elemento não encontrado, timeout),
retorna falha explícita com o motivo genuíno.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class BrowserActionResult:
    success: bool
    final_url: str | None = None
    page_title: str | None = None
    screenshot_path: str | None = None
    duration_seconds: float = 0.0
    reason: str = ""


class BrowserExecutor:
    """Executor REAL de navegador headless. Cada método abre um
    navegador de verdade, executa a ação, e SEMPRE fecha o navegador
    ao final (sucesso ou falha) — nunca deixa um processo de
    navegador órfão."""

    def __init__(self, *, headless: bool = True, timeout_seconds: float = 15.0) -> None:
        self.headless = headless
        self.timeout_seconds = timeout_seconds

    def is_browser_available(self) -> tuple:
        """MICROCORREÇÃO (item 4): verificação EXPLÍCITA e rápida —
        confirma que a biblioteca Playwright está instalada E que o
        binário do Chromium existe no caminho esperado, ANTES de
        tentar lançar. Devolve `(True, "")` quando genuinamente
        disponível, ou `(False, motivo)` quando não — nunca afirma
        disponibilidade sem checar o caminho real do executável no
        sistema de arquivos deste processo."""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return False, "biblioteca playwright não instalada neste ambiente (pip install playwright)"
        try:
            with sync_playwright() as p:
                executable_path = p.chromium.executable_path
        except Exception as exc:
            return False, f"falha ao consultar o caminho do executável do Chromium via Playwright: {exc}"
        import os

        if not executable_path or not os.path.exists(executable_path):
            return False, (
                f"binário do Chromium não encontrado em '{executable_path}' — rode "
                f"'playwright install chromium' (ver requirements.txt)"
            )
        return True, ""

    def navigate_and_extract(self, *, url: str, screenshot_dir: str | None = None) -> BrowserActionResult:
        from core.operations.egress import egress_blocked   # v1.0.3: RadarReal e NavigatorReal passam aqui

        _blocked = egress_blocked(url)
        if _blocked:
            return BrowserActionResult(success=False, reason=_blocked)
        """Navega para uma URL real e extrai título/conteúdo —
        equivalente ao "fallback de navegador" para descoberta/leitura
        quando não há API oficial. Nunca finge ter navegado: falha de
        rede real (sem conectividade neste ambiente) é reportada com
        o motivo genuíno do Playwright; binário ausente é detectado
        ANTES de tentar, com motivo específico e acionável."""
        try:
            from playwright.sync_api import Error as PlaywrightError
            from playwright.sync_api import sync_playwright
        except ImportError:
            return BrowserActionResult(success=False, reason="biblioteca playwright não disponível neste ambiente")

        available, reason = self.is_browser_available()
        if not available:
            return BrowserActionResult(success=False, reason=reason)

        start = time.time()
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=self.headless)
                try:
                    page = browser.new_page()
                    _last = egress_blocked(url)   # v1.0.4: última checagem imediatamente antes da navegação
                    if _last:
                        return BrowserActionResult(success=False, reason=_last)
                    page.goto(url, timeout=self.timeout_seconds * 1000)
                    title = page.title()
                    screenshot_path = None
                    if screenshot_dir:
                        import os
                        import uuid

                        os.makedirs(screenshot_dir, exist_ok=True)
                        screenshot_path = os.path.join(screenshot_dir, f"shot_{uuid.uuid4().hex[:10]}.png")
                        page.screenshot(path=screenshot_path)
                    return BrowserActionResult(
                        success=True, final_url=page.url, page_title=title, screenshot_path=screenshot_path,
                        duration_seconds=time.time() - start,
                    )
                finally:
                    browser.close()
        except PlaywrightError as exc:
            return BrowserActionResult(
                success=False, reason=f"falha real de navegação: {exc}", duration_seconds=time.time() - start,
            )
        except Exception as exc:
            return BrowserActionResult(
                success=False, reason=f"falha inesperada no executor de navegador real: {exc}",
                duration_seconds=time.time() - start,
            )

    def execute_authenticated_action(
        self, *, url: str, storage_state_path: str | None, action_script: str,
    ) -> BrowserActionResult:
        """Executa uma ação autenticada real (sessão previamente salva
        via `storage_state_path` — cookies/localStorage reais de um
        login legítimo do Owner, nunca credenciais adivinhadas) —
        usado para plataformas sem API oficial viável onde uma sessão
        de navegador autenticada é o único caminho legítimo. Requer
        aprovação prévia do Owner para a ação em si (verificada pelo
        chamador antes de invocar este método — nunca autoexecutado)."""
        from core.operations.egress import egress_blocked   # v1.0.3: ação autenticada no navegador

        _blocked = egress_blocked(url)
        if _blocked:
            return BrowserActionResult(success=False, reason=_blocked)
        try:
            from playwright.sync_api import Error as PlaywrightError
            from playwright.sync_api import sync_playwright
        except ImportError:
            return BrowserActionResult(success=False, reason="biblioteca playwright não disponível neste ambiente")

        available, reason = self.is_browser_available()
        if not available:
            return BrowserActionResult(success=False, reason=reason)

        start = time.time()
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=self.headless)
                try:
                    context_kwargs = {"storage_state": storage_state_path} if storage_state_path else {}
                    context = browser.new_context(**context_kwargs)
                    page = context.new_page()
                    _last = egress_blocked(url)   # v1.0.4: última checagem imediatamente antes da navegação
                    if _last:
                        return BrowserActionResult(success=False, reason=_last)
                    page.goto(url, timeout=self.timeout_seconds * 1000)
                    # CORREÇÃO: nunca usa eval() genérico sobre string
                    # arbitrária de fora -- action_script é executado
                    # apenas como JS de página (page.evaluate), nunca
                    # como comando do sistema operacional; o CHAMADOR é
                    # responsável por essa string vir de um catálogo de
                    # ações pré-aprovadas, nunca de entrada de usuário livre.
                    _last = egress_blocked(url)   # v1.0.5: checagem imediatamente antes da AÇÃO mutável
                    if _last:
                        return BrowserActionResult(success=False, reason=_last)
                    page.evaluate(action_script)
                    return BrowserActionResult(
                        success=True, final_url=page.url, page_title=page.title(),
                        duration_seconds=time.time() - start,
                    )
                finally:
                    browser.close()
        except PlaywrightError as exc:
            return BrowserActionResult(
                success=False, reason=f"falha real ao executar ação autenticada: {exc}",
                duration_seconds=time.time() - start,
            )
        except Exception as exc:
            return BrowserActionResult(
                success=False, reason=f"falha inesperada no executor de navegador real: {exc}",
                duration_seconds=time.time() - start,
            )
