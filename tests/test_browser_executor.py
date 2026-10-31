import unittest

from bridge.execution.browser_executor import BrowserExecutor

"""
Testes BLOCO FINAL 2 (item 1/5 — navegador automatizado real):
navegação genuína via Playwright. Número de testes DELIBERADAMENTE
limitado — cada teste lança um processo real de navegador
(~0.7-1s) — e usa apenas URLs `data:` (sem rede) ou domínios
propositalmente inválidos (falha rápida de DNS), nunca uma URL
externa real, preservando a disciplina de suite-order-stability
(nunca depender do comportamento de rede do ambiente).

MICROCORREÇÃO BF2 (item 1 — SKIP explícito sem Chromium): cada teste
verifica `is_browser_available()` ANTES de exercitar o navegador e
usa `self.skipTest(...)` quando o binário não está presente — nunca
um FALSE FAIL (que aconteceria se o teste simplesmente assumisse
sucesso) nem um PASS enganoso pelo motivo errado (que aconteceria em
`test_invalid_domain_fails_honestly_never_fake_success`, cuja
asserção `success=False` também seria satisfeita por um Chromium
ausente, mascarando que o teste não exercitou DNS real nenhum).
"""


class BrowserExecutorTests(unittest.TestCase):
    def setUp(self):
        self.executor = BrowserExecutor(headless=True, timeout_seconds=8.0)
        available, reason = self.executor.is_browser_available()
        if not available:
            self.skipTest(f"binário do Chromium não disponível neste ambiente: {reason}")

    def test_navigates_and_extracts_real_title(self):
        result = self.executor.navigate_and_extract(
            url="data:text/html,<html><head><title>Titulo Real</title></head><body>x</body></html>",
        )
        self.assertTrue(result.success)
        self.assertEqual(result.page_title, "Titulo Real")

    def test_invalid_domain_fails_honestly_never_fake_success(self):
        result = self.executor.navigate_and_extract(url="http://dominio-teste-invalido-navegador.invalid")
        self.assertFalse(result.success)
        self.assertTrue(result.reason)


if __name__ == "__main__":
    unittest.main()
