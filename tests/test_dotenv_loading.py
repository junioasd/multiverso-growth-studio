import os
import subprocess
import sys
import tempfile
import unittest
from tests._auth_test_helpers import fast_hash_password

"""
Testes de regressão — MICROCORREÇÃO 3, item 5
(CARREGAMENTO DA CONFIGURAÇÃO).

Falha original: o guia orientava preencher `.env`, mas o programa só
consultava `os.environ` diretamente — nada carregava o arquivo.

Estes testes rodam um PROCESSO SEPARADO de verdade (`subprocess`), com
um ambiente LIMPO (sem herdar OWNER_EMAIL/etc. do processo de teste) e
um `.env` TEMPORÁRIO com credenciais SINTÉTICAS, para confirmar que
`core.config.SETTINGS` carrega o arquivo de fato — sem depender de
nada já definido no ambiente de desenvolvimento.
"""

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _run_in_clean_process(cwd: str, code: str) -> str:
    """Executa `code` num processo Python totalmente novo, com um
    ambiente mínimo (sem herdar variáveis do processo de teste) e cwd
    apontando para `cwd` (onde um `.env` sintético pode existir)."""
    clean_env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": PROJECT_ROOT,
    }
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=cwd, env=clean_env, capture_output=True, text=True, timeout=15,
    )
    if result.returncode != 0:
        raise AssertionError(f"processo filho falhou:\nSTDOUT:{result.stdout}\nSTDERR:{result.stderr}")
    return result.stdout


class DotenvLoadingTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _write_env(self, content: str) -> None:
        with open(os.path.join(self.tmp_dir, ".env"), "w", encoding="utf-8") as f:
            f.write(content)

    def test_owner_email_loaded_from_dotenv_in_fresh_process(self):
        self._write_env("OWNER_EMAIL=synthetic-owner@fresh-process.test\n")
        out = _run_in_clean_process(
            self.tmp_dir,
            "from core.config import SETTINGS; print(SETTINGS.owner_email)",
        )
        self.assertEqual(out.strip(), "synthetic-owner@fresh-process.test")

    def test_owner_password_hash_dollar_signs_preserved_literally(self):
        # hash sintético (NÃO É UMA CREDENCIAL REAL) — o importante é
        # que os `$` sejam preservados exatamente, sem expansão de shell.
        synthetic_hash = "pbkdf2_sha256$260000$c3ludGhldGlj$ZmFrZS1oYXNoLXZhbHVl"
        self._write_env(f"OWNER_PASSWORD_HASH={synthetic_hash}\n")
        out = _run_in_clean_process(
            self.tmp_dir,
            "from core.config import SETTINGS; print(SETTINGS.owner_password_hash)",
        )
        self.assertEqual(out.strip(), synthetic_hash)

    def test_db_path_loaded_from_dotenv(self):
        self._write_env("DB_PATH=./synthetic-data/test.db\n")
        out = _run_in_clean_process(
            self.tmp_dir,
            "from core.config import SETTINGS; print(SETTINGS.db_path)",
        )
        self.assertEqual(out.strip(), "./synthetic-data/test.db")

    def test_session_secret_loaded_from_dotenv(self):
        self._write_env("SESSION_SECRET=synthetic-fresh-process-secret-value\n")
        out = _run_in_clean_process(
            self.tmp_dir,
            "from core.config import SETTINGS; print(SETTINGS.resolved_session_secret())",
        )
        self.assertEqual(out.strip(), "synthetic-fresh-process-secret-value")

    def test_all_four_documented_variables_loaded_together(self):
        synthetic_hash = "pbkdf2_sha256$260000$YWJj$ZGVm"
        self._write_env(
            "OWNER_EMAIL=owner-combo@fresh.test\n"
            f"OWNER_PASSWORD_HASH={synthetic_hash}\n"
            "DB_PATH=./combo-data/db.sqlite\n"
            "SESSION_SECRET=combo-secret-xyz\n"
        )
        out = _run_in_clean_process(
            self.tmp_dir,
            "from core.config import SETTINGS\n"
            "print(SETTINGS.owner_email)\n"
            "print(SETTINGS.owner_password_hash)\n"
            "print(SETTINGS.db_path)\n"
            "print(SETTINGS.resolved_session_secret())\n",
        )
        lines = out.strip().splitlines()
        self.assertEqual(lines[0], "owner-combo@fresh.test")
        self.assertEqual(lines[1], synthetic_hash)
        self.assertEqual(lines[2], "./combo-data/db.sqlite")
        self.assertEqual(lines[3], "combo-secret-xyz")

    def test_real_environment_variable_takes_precedence_over_dotenv(self):
        self._write_env("OWNER_EMAIL=from-dotenv@fresh.test\n")
        clean_env = {
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": PROJECT_ROOT,
            "OWNER_EMAIL": "from-real-environment@fresh.test",  # já definida no ambiente real
        }
        result = subprocess.run(
            [sys.executable, "-c", "from core.config import SETTINGS; print(SETTINGS.owner_email)"],
            cwd=self.tmp_dir, env=clean_env, capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "from-real-environment@fresh.test")

    def test_no_dotenv_file_falls_back_to_defaults_without_crashing(self):
        # tmp_dir sem nenhum .env
        out = _run_in_clean_process(
            self.tmp_dir,
            "from core.config import SETTINGS; print(SETTINGS.owner_email)",
        )
        self.assertEqual(out.strip(), "owner@example.com")  # default documentado

    def test_dotenv_never_executes_arbitrary_content(self):
        # Uma linha maliciosa/parecida com comando deve ser tratada como
        # DADO literal (valor de string), nunca executada.
        self._write_env('OWNER_EMAIL=$(echo pwned)\n')
        out = _run_in_clean_process(
            self.tmp_dir,
            "from core.config import SETTINGS; print(SETTINGS.owner_email)",
        )
        self.assertEqual(out.strip(), "$(echo pwned)")  # literal, não executado

    def test_full_app_bootstrap_uses_dotenv_loaded_credentials(self):
        synthetic_hash_source = "SyntheticFreshProcessPass123"
        code = (
            "from core.security.auth import hash_password\n"
            f"print(hash_password({synthetic_hash_source!r}))\n"
        )
        password_hash = _run_in_clean_process(self.tmp_dir, code).strip()

        self._write_env(
            "OWNER_EMAIL=owner-bootstrap@fresh.test\n"
            f"OWNER_PASSWORD_HASH={password_hash}\n"
        )
        db_dir = os.path.join(self.tmp_dir, "data")
        os.makedirs(db_dir, exist_ok=True)
        with open(os.path.join(self.tmp_dir, ".env"), "a", encoding="utf-8") as f:
            f.write(f"DB_PATH={os.path.join(db_dir, 'test.db')}\n")

        code = (
            "from api.bootstrap import build_app\n"
            "app = build_app()\n"
            "print(app.auth.owner_email)\n"
            f"print(app.auth.login('owner-bootstrap@fresh.test', {synthetic_hash_source!r}) is not None)\n"
        )
        out = _run_in_clean_process(self.tmp_dir, code)
        lines = out.strip().splitlines()
        self.assertEqual(lines[0], "owner-bootstrap@fresh.test")
        self.assertEqual(lines[1], "True")


if __name__ == "__main__":
    unittest.main()
