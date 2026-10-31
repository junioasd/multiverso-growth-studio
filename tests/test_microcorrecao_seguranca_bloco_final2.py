import ast
import glob
import os
import threading
import unittest
from unittest.mock import patch

from atlas.escalation import EscalationDesk
from bridge.execution.browser_executor import BrowserExecutor
from bridge.execution.navigator_real import ActionCriticality, NavigatorReal
from bridge.execution.spend_guard import SpendContextMismatch, SpendGuard, SpendNotApproved
from bridge.hub import BridgeHub
from core.security.audit import AuditTrail
from core.security.auth import Principal, Role

"""
MICROCORREÇÃO — BLOCO FINAL 2 / SEGURANÇA + EXECUÇÃO REPRODUZÍVEL.
Testes de regressão independentes para as 3 falhas confirmadas pela
auditoria e corrigidas nesta entrega — cada teste aqui deveria FALHAR
contra o código ANTES desta correção.
"""

OWNER = Principal(subject="owner@x.com", role=Role.OWNER, scopes={"*"})


class OwnerApprovalBindingRegressionTests(unittest.TestCase):
    """Item 1 — uma aprovação nunca pode ser reaproveitada para um
    contexto diferente do originalmente aprovado."""

    def setUp(self):
        self.audit = AuditTrail()
        self.hub = BridgeHub()
        self.desk = EscalationDesk(self.audit)
        self.nav = NavigatorReal(bridge_hub=self.hub, escalation_desk=self.desk, audit=self.audit)

    def _approved_escalation_for(self, *, platform="youtube", action="PUBLISH_VIDEO", channel="canalA", actor="ARGOS"):
        blocked = self.nav.execute_action(
            platform=platform, action=action, criticality=ActionCriticality.CRITICAL,
            params={"channel_id": channel}, actor=actor,
        )
        self.desk.approve(blocked.escalation_id, decision="aprovado", resolved_by=OWNER)
        return blocked.escalation_id

    def test_reuse_with_different_channel_rejected(self):
        esc_id = self._approved_escalation_for(channel="canalA")
        result = self.nav.execute_action(
            platform="youtube", action="PUBLISH_VIDEO", criticality=ActionCriticality.CRITICAL,
            params={"channel_id": "canalB"}, actor="ARGOS", approved_escalation_id=esc_id,
        )
        self.assertFalse(result.success)
        self.assertIn("contexto DIFERENTE", result.reason)

    def test_reuse_with_different_action_rejected(self):
        esc_id = self._approved_escalation_for(action="PUBLISH_VIDEO")
        result = self.nav.execute_action(
            platform="youtube", action="DELETE_VIDEO", criticality=ActionCriticality.CRITICAL,
            params={"channel_id": "canalA"}, actor="ARGOS", approved_escalation_id=esc_id,
        )
        self.assertFalse(result.success)

    def test_reuse_with_different_platform_rejected(self):
        esc_id = self._approved_escalation_for(platform="youtube")
        result = self.nav.execute_action(
            platform="facebook", action="PUBLISH_VIDEO", criticality=ActionCriticality.CRITICAL,
            params={"channel_id": "canalA"}, actor="ARGOS", approved_escalation_id=esc_id,
        )
        self.assertFalse(result.success)

    def test_reuse_with_different_actor_rejected(self):
        esc_id = self._approved_escalation_for(actor="ARGOS")
        result = self.nav.execute_action(
            platform="youtube", action="PUBLISH_VIDEO", criticality=ActionCriticality.CRITICAL,
            params={"channel_id": "canalA"}, actor="AGENTE_MALICIOSO", approved_escalation_id=esc_id,
        )
        self.assertFalse(result.success)

    def test_exact_original_context_still_works(self):
        esc_id = self._approved_escalation_for(channel="canalA")
        result = self.nav.execute_action(
            platform="youtube", action="PUBLISH_VIDEO", criticality=ActionCriticality.CRITICAL,
            params={"channel_id": "canalA"}, actor="ARGOS", approved_escalation_id=esc_id,
        )
        # passa da checagem de aprovação (falha adiante por falta de mecanismo real, nunca por aprovação)
        self.assertNotIn("contexto DIFERENTE", result.reason)
        self.assertNotIn("não está APPROVED", result.reason)


class SpendGuardContextBindingRegressionTests(unittest.TestCase):
    """Item 2 — uma aprovação de gasto nunca pode ser reaproveitada
    para outra campanha/plataforma/moeda."""

    def setUp(self):
        self.audit = AuditTrail()
        self.desk = EscalationDesk(self.audit)
        self.guard = SpendGuard(escalation_desk=self.desk)

    def _approved_escalation(self, *, platform="google_ads", campaign="camp1", currency="BRL", amount=100.0):
        esc_id = self.guard.request_spend_approval(
            platform=platform, campaign_reference=campaign, requested_amount=amount, currency=currency,
            actor="ARGOS", reason="teste",
        )
        self.desk.approve(esc_id, decision="ok", resolved_by=OWNER)
        return esc_id

    def test_swap_platform_after_approval_rejected(self):
        esc_id = self._approved_escalation(platform="google_ads")
        with self.assertRaises(SpendContextMismatch):
            self.guard.confirm_approval(escalation_id=esc_id, platform="facebook_ads", campaign_reference="camp1", currency="BRL")

    def test_swap_campaign_after_approval_rejected(self):
        esc_id = self._approved_escalation(campaign="camp1")
        with self.assertRaises(SpendContextMismatch):
            self.guard.confirm_approval(escalation_id=esc_id, platform="google_ads", campaign_reference="camp_outra", currency="BRL")

    def test_swap_currency_after_approval_rejected(self):
        esc_id = self._approved_escalation(currency="BRL")
        with self.assertRaises(SpendContextMismatch):
            self.guard.confirm_approval(escalation_id=esc_id, platform="google_ads", campaign_reference="camp1", currency="USD")

    def test_exact_original_context_still_works(self):
        esc_id = self._approved_escalation(platform="google_ads", campaign="camp1", currency="BRL", amount=100.0)
        approval = self.guard.confirm_approval(escalation_id=esc_id, platform="google_ads", campaign_reference="camp1", currency="BRL")
        self.assertEqual(approval.approved_amount, 100.0)


class SpendGuardNegativeSpendRegressionTests(unittest.TestCase):
    """Item 2 — gasto negativo/zero nunca pode inflar orçamento restante."""

    def setUp(self):
        self.audit = AuditTrail()
        self.desk = EscalationDesk(self.audit)
        self.guard = SpendGuard(escalation_desk=self.desk)
        esc_id = self.guard.request_spend_approval(
            platform="google_ads", campaign_reference="camp1", requested_amount=100.0, currency="BRL",
            actor="ARGOS", reason="teste",
        )
        self.desk.approve(esc_id, decision="ok", resolved_by=OWNER)
        self.approval = self.guard.confirm_approval(escalation_id=esc_id, platform="google_ads", campaign_reference="camp1", currency="BRL")

    def test_negative_amount_rejected(self):
        with self.assertRaises(ValueError):
            self.guard.record_spend(approval_id=self.approval.approval_id, amount=-50.0)

    def test_zero_amount_rejected(self):
        with self.assertRaises(ValueError):
            self.guard.record_spend(approval_id=self.approval.approval_id, amount=0.0)

    def test_negative_spend_never_inflates_remaining(self):
        self.guard.record_spend(approval_id=self.approval.approval_id, amount=50.0)
        remaining_before = self.approval.remaining()
        try:
            self.guard.record_spend(approval_id=self.approval.approval_id, amount=-1000.0)
        except ValueError:
            pass
        self.assertEqual(self.approval.remaining(), remaining_before)


class SpendGuardOverspendRegressionTests(unittest.TestCase):
    """Item 2 — nunca é possível gastar além do valor aprovado,
    inclusive sob concorrência real."""

    def setUp(self):
        self.audit = AuditTrail()
        self.desk = EscalationDesk(self.audit)
        self.guard = SpendGuard(escalation_desk=self.desk)
        esc_id = self.guard.request_spend_approval(
            platform="google_ads", campaign_reference="camp1", requested_amount=100.0, currency="BRL",
            actor="ARGOS", reason="teste",
        )
        self.desk.approve(esc_id, decision="ok", resolved_by=OWNER)
        self.approval = self.guard.confirm_approval(escalation_id=esc_id, platform="google_ads", campaign_reference="camp1", currency="BRL")

    def test_single_overspend_rejected(self):
        self.guard.record_spend(approval_id=self.approval.approval_id, amount=100.0)
        with self.assertRaises(SpendNotApproved):
            self.guard.record_spend(approval_id=self.approval.approval_id, amount=0.01)

    def test_concurrent_spend_never_exceeds_approved_amount(self):
        """Corrida real com 30 threads tentando gastar 10 cada
        (300 no total, aprovado apenas 100) — o resultado final NUNCA
        pode ultrapassar 100.0, mesmo sob concorrência genuína."""
        errors = []
        successes = []

        def worker():
            try:
                self.guard.record_spend(approval_id=self.approval.approval_id, amount=10.0)
                successes.append(1)
            except SpendNotApproved:
                errors.append(1)

        threads = [threading.Thread(target=worker) for _ in range(30)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertLessEqual(self.approval.spent_so_far, 100.0)
        self.assertEqual(len(successes), 10)
        self.assertEqual(len(errors), 20)


class RequirementsCompletenessRegressionTests(unittest.TestCase):
    """Item 3 — requirements.txt precisa cobrir TODAS as dependências
    de terceiros genuinamente importadas pelo código — nunca depender
    de biblioteca "assumida como já instalada"."""

    _KNOWN_STDLIB = {
        'os', 'sys', 'json', 'time', 'uuid', 'dataclasses', 'enum', 'typing', 'threading', 'sqlite3',
        'hashlib', 'hmac', 'base64', 'urllib', 're', 'io', 'wave', 'audioop', 'statistics', 'collections',
        'functools', 'itertools', 'abc', 'contextlib', 'pathlib', 'tempfile', 'unittest', 'subprocess',
        'warnings', 'gc', 'copy', 'random', 'string', 'struct', 'html', 'datetime', 'math', 'traceback',
        'logging', 'inspect', 'weakref', 'shutil', 'glob', 'csv', 'email', 'socket', 'ssl', 'http', 'queue',
        'multiprocessing', 'signal', 'errno', 'platform', 'configparser', 'argparse', 'importlib', 'ast',
        'getpass', 'secrets', 'socketserver', 'concurrent',
        # 'ctypes' e stdlib do CPython (usado pela correcao P1 do
        # GraphExecutor para provar a morte do detentor do lease via SO,
        # no Windows); o pacote 'ctypes' no PyPI e um shim quebrado --
        # nunca deve ir para requirements.txt.
        'ctypes',
        # 'py_compile' e stdlib do CPython (sys.stdlib_module_names;
        # Lib/py_compile.py) -- usado pelo teste de compilacao do
        # Comando 2/6. Ausente desta lista, gerava FALSO POSITIVO.
        'py_compile',
        # 'types' e stdlib do CPython (sys.stdlib_module_names; Lib/types.py) --
        # usado pelos testes da correcao pos-auditoria. Mesmo falso positivo.
        'types',
    }
    # mapeia o nome do modulo importavel para o nome real do pacote pip
    # (nem sempre coincidem -- ex: PIL vem do pacote "Pillow")
    _IMPORT_TO_PACKAGE = {
        "PIL": "Pillow", "cv2": "opencv-python", "docx": "python-docx", "cryptography": "cryptography",
        "oauthlib": "oauthlib", "playwright": "playwright", "pypdf": "pypdf", "numpy": "numpy",
    }

    def _project_local_packages(self) -> set:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return {
            name for name in os.listdir(root)
            if os.path.isdir(os.path.join(root, name)) and os.path.isfile(os.path.join(root, name, "__init__.py"))
        }

    def _scan_third_party_imports(self) -> set:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        local_packages = self._project_local_packages()
        third_party = set()
        for path in glob.glob(os.path.join(root, "**", "*.py"), recursive=True):
            if "__pycache__" in path:
                continue
            with open(path, encoding="utf-8") as f:
                try:
                    tree = ast.parse(f.read(), filename=path)
                except SyntaxError:
                    continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        top = alias.name.split(".")[0]
                        if top not in self._KNOWN_STDLIB and top not in local_packages and not top.startswith("_"):
                            third_party.add(top)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    top = node.module.split(".")[0]
                    if top not in self._KNOWN_STDLIB and top not in local_packages and not top.startswith("_"):
                        third_party.add(top)
        return third_party

    def test_every_third_party_import_listed_in_requirements(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "requirements.txt"), encoding="utf-8") as f:
            requirements_content = f.read().lower()

        third_party_imports = self._scan_third_party_imports()
        missing = []
        for import_name in third_party_imports:
            package_name = self._IMPORT_TO_PACKAGE.get(import_name, import_name)
            if package_name.lower() not in requirements_content:
                missing.append((import_name, package_name))

        self.assertEqual(
            missing, [],
            f"import(s) de terceiros usados no código mas ausentes de requirements.txt: {missing}",
        )

    def test_requirements_file_has_no_unused_entries_beyond_known(self):
        """Confirma que os pacotes REALMENTE usados (PIL/cv2/docx/
        cryptography/oauthlib/playwright/pypdf/numpy) estão todos
        listados — checagem positiva complementar à anterior."""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "requirements.txt"), encoding="utf-8") as f:
            content = f.read().lower()
        for package_name in ("pillow", "pypdf", "python-docx", "opencv-python", "numpy", "cryptography", "oauthlib", "playwright"):
            self.assertIn(package_name, content, f"'{package_name}' esperado em requirements.txt")


class BrowserBinaryDetectionRegressionTests(unittest.TestCase):
    """Item 4 — ausência do binário do Playwright é detectada
    explicitamente e retorna UNAVAILABLE, nunca uma exceção crua ou
    um falso positivo de sucesso."""

    def test_missing_binary_detected_before_launch_attempt(self):
        executor = BrowserExecutor()
        with patch("os.path.exists", return_value=False):
            available, reason = executor.is_browser_available()
        self.assertFalse(available)
        self.assertIn("playwright install chromium", reason)

    def test_navigate_returns_clean_unavailable_when_binary_missing(self):
        executor = BrowserExecutor()
        with patch.object(BrowserExecutor, "is_browser_available", return_value=(False, "binário ausente (simulado)")):
            result = executor.navigate_and_extract(url="https://x.example")
        self.assertFalse(result.success)
        self.assertIn("ausente", result.reason)

    def test_real_browser_works_after_documented_setup(self):
        """Único teste real (sem mock) desta classe — confirma que,
        NESTE ambiente (onde o passo 'playwright install chromium' já
        foi executado, conforme documentado em requirements.txt), o
        navegador real funciona de ponta a ponta com uma URL data:."""
        executor = BrowserExecutor(headless=True, timeout_seconds=8.0)
        available, reason = executor.is_browser_available()
        if not available:
            self.skipTest(f"binário do Chromium não disponível neste ambiente de teste: {reason}")
        result = executor.navigate_and_extract(url="data:text/html,<html><title>Setup Verificado</title></html>")
        self.assertTrue(result.success)
        self.assertEqual(result.page_title, "Setup Verificado")


if __name__ == "__main__":
    unittest.main()
