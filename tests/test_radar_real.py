import unittest
from unittest.mock import patch

from bridge.execution.browser_executor import BrowserActionResult, BrowserExecutor
from bridge.execution.radar_real import OpportunityValidationStatus, RadarReal

"""
Testes BLOCO FINAL 2 (item 3 — RADAR REAL): nunca inventa uma
oportunidade, sempre tenta um acesso real antes de validar. Usa
`BrowserExecutor.navigate_and_extract` mockado para o caminho de
sucesso (evitando depender de rede externa real ou lançar múltiplos
processos de navegador em cada teste — mesma disciplina de
suite-order-stability).
"""


class RadarRealTests(unittest.TestCase):
    def test_invalid_url_never_attempted(self):
        radar = RadarReal()
        with patch.object(BrowserExecutor, "navigate_and_extract") as mock_navigate:
            result = radar.discover(candidate_url="nao-eh-uma-url-real")
        self.assertEqual(result.validation_status, OpportunityValidationStatus.INVALID_URL)
        mock_navigate.assert_not_called()

    def test_unavailable_source_reported_honestly(self):
        radar = RadarReal()
        fake_failure = BrowserActionResult(success=False, reason="falha real de navegação simulada")
        with patch.object(BrowserExecutor, "navigate_and_extract", return_value=fake_failure):
            result = radar.discover(candidate_url="https://fonte-teste.example/produto")
        self.assertEqual(result.validation_status, OpportunityValidationStatus.SOURCE_UNAVAILABLE)
        self.assertIn("falha real", result.notes)

    def test_validated_opportunity_has_real_evidence(self):
        radar = RadarReal()
        fake_success = BrowserActionResult(
            success=True, final_url="https://marketplace.example/produto123", page_title="Produto Real",
        )
        with patch.object(BrowserExecutor, "navigate_and_extract", return_value=fake_success):
            result = radar.discover(candidate_url="https://marketplace.example/produto123", niche="fitness")
        self.assertEqual(result.validation_status, OpportunityValidationStatus.VALIDATED)
        self.assertIsNotNone(result.evidence_hash)
        self.assertEqual(result.platform_or_domain, "marketplace.example")

    def test_validated_opportunities_filtered_by_niche(self):
        radar = RadarReal()
        fake_success = BrowserActionResult(success=True, final_url="https://x.example/a", page_title="x")
        with patch.object(BrowserExecutor, "navigate_and_extract", return_value=fake_success):
            radar.discover(candidate_url="https://x.example/a", niche="fitness")
            radar.discover(candidate_url="https://x.example/b", niche="financas")
        self.assertEqual(len(radar.validated_opportunities(niche="fitness")), 1)
        self.assertEqual(len(radar.validated_opportunities(niche="financas")), 1)
        self.assertEqual(len(radar.validated_opportunities()), 2)

    def test_discover_many_never_invents_missing_sources(self):
        radar = RadarReal()
        fake_failure = BrowserActionResult(success=False, reason="indisponível (simulado)")
        with patch.object(BrowserExecutor, "navigate_and_extract", return_value=fake_failure):
            results = radar.discover_many(candidate_urls=["https://a.example", "https://b.example"])
        self.assertEqual(len(results), 2)
        for r in results:
            self.assertEqual(r.validation_status, OpportunityValidationStatus.SOURCE_UNAVAILABLE)

    def test_real_browser_used_for_genuinely_invalid_domain(self):
        """Único teste desta classe que lança um navegador real —
        confirma que a integração de ponta a ponta (sem nenhum mock)
        também funciona, com um domínio propositalmente inválido para
        falhar rápido via DNS.

        MICROCORREÇÃO BF2 (item 1): pula explicitamente sem Chromium
        — sem essa checagem, o teste "passaria" mesmo sem navegador
        real algum, já que `is_browser_available()==False` também
        produz `SOURCE_UNAVAILABLE`, mascarando que nenhuma tentativa
        de DNS de verdade ocorreu."""
        executor = BrowserExecutor(headless=True, timeout_seconds=5.0)
        available, reason = executor.is_browser_available()
        if not available:
            self.skipTest(f"binário do Chromium não disponível neste ambiente: {reason}")
        radar = RadarReal(browser_executor=executor)
        result = radar.discover(candidate_url="http://dominio-radar-real-inexistente-teste.invalid")
        self.assertEqual(result.validation_status, OpportunityValidationStatus.SOURCE_UNAVAILABLE)


if __name__ == "__main__":
    unittest.main()
