import ast
import glob
import os
import unittest

"""
Teste de regressão — MICROCORREÇÃO FINAL (ResourceWarnings residuais).

Causa raiz encontrada: vários testes de migração (`test_migration_v6_
recovery.py`, `test_migration_atomicity.py`) usavam
`with sqlite3.connect(...) as conn:` diretamente. Diferente de
`core.models.db.connect()` (um context manager PRÓPRIO que sempre
fecha via `finally: conn.close()`), o context manager NATIVO do
`sqlite3.Connection` SÓ cuida de commit/rollback ao sair do bloco —
NUNCA fecha a conexão. Isso deixava conexões abertas, dependendo
apenas da coleta de lixo para finalmente emitir `ResourceWarning`.

Corrigido substituindo esses usos por `with db_module.connect(...) as
conn:`, que sempre fecha (sucesso, erro, e rollback).

Este teste garante que o padrão perigoso (`with sqlite3.connect(...)`
sem fechamento explícito em nenhum outro lugar da função) nunca mais
aparece em produção ou em testes — nem por acidente numa futura
edição.
"""


def _iter_python_files():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for path in glob.glob(os.path.join(root, "**", "*.py"), recursive=True):
        if "__pycache__" in path:
            continue
        yield path


class NoUnclosedSqliteConnectionsRegressionTests(unittest.TestCase):
    """Checagem estática (AST) — nunca depende de rodar o código, então
    pega o padrão perigoso mesmo em caminhos de erro raramente
    exercitados pelos testes."""

    def test_no_bare_with_sqlite3_connect_blocks(self):
        """`with sqlite3.connect(...) as conn:` (sem passar por
        `db_module.connect()`) NUNCA fecha a conexão sozinho — é
        sempre um bug. Verifica isso via análise de texto simples
        (mais robusta que AST para este padrão específico, já que
        cobre qualquer formatação)."""
        offenders = []
        for path in _iter_python_files():
            try:
                with open(path, encoding="utf-8") as f:
                    content = f.read()
            except (UnicodeDecodeError, OSError):
                continue
            for lineno, line in enumerate(content.split("\n"), start=1):
                stripped = line.strip()
                if stripped.startswith("with sqlite3.connect("):
                    offenders.append(f"{os.path.relpath(path)}:{lineno}")
        self.assertEqual(
            offenders, [],
            f"'with sqlite3.connect(...)' encontrado (nunca fecha a conexão sozinho — "
            f"use 'with db_module.connect(...)' em vez disso): {offenders}",
        )

    def test_every_function_that_opens_a_raw_connection_closes_it_or_hands_it_off(self):
        """Para toda função que chama `sqlite3.connect()`/
        `get_connection()`/`get_autocommit_connection()` diretamente
        (fora de `core.models.db` em si, onde essas são as próprias
        fábricas): ou fecha a conexão explicitamente (`.close()`)
        dentro da mesma função, ou explicitamente devolve a conexão/
        uma fábrica para o chamador assumir a responsabilidade
        (`return conn`/`return factory`/`return row_conn`)."""
        offenders = []
        for path in _iter_python_files():
            rel = os.path.relpath(path)
            if rel == os.path.join("core", "models", "db.py"):
                continue  # as próprias fábricas/context managers vivem aqui
            if rel == os.path.join("tests", "test_correction_final_resource_cleanup.py"):
                continue  # este próprio arquivo menciona o padrão como texto, não como código real
            try:
                with open(path, encoding="utf-8") as f:
                    source = f.read()
                tree = ast.parse(source)
            except (SyntaxError, UnicodeDecodeError, OSError):
                continue
            lines = source.split("\n")
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                func_source = "\n".join(lines[node.lineno - 1:node.end_lineno])
                connect_count = (
                    func_source.count("sqlite3.connect(")
                    + func_source.count("get_connection(")
                    + func_source.count("get_autocommit_connection(")
                )
                if connect_count == 0:
                    continue
                close_count = func_source.count(".close()")
                hands_off = (
                    "return conn" in func_source or "return factory" in func_source
                    or "return row_conn" in func_source
                )
                if close_count < connect_count and not hands_off:
                    offenders.append(f"{rel}:{node.lineno} ({node.name})")
        self.assertEqual(
            offenders, [],
            f"função(ões) que abrem conexão SQLite sem fechar explicitamente nem devolver "
            f"a responsabilidade ao chamador: {offenders}",
        )


class RuntimeNoResourceWarningsRegressionTests(unittest.TestCase):
    """Checagem em tempo de execução — roda um subconjunto representativo
    dos testes de migração (os que motivaram esta correção) com
    ResourceWarning promovido a erro; qualquer conexão esquecida
    aborta o teste imediatamente."""

    def test_migration_tests_produce_no_resource_warnings(self):
        # ORCAMENTO DE TEMPO (ajuste de teto; producao intocada): 60s -> 180s.
        # Medicao nesta maquina (3 execucoes do comando exato abaixo, com
        # -W error::ResourceWarning): 126.9s / 127.8s / 125.9s (61/61 OK,
        # media 126.9s, dispersao +-0.75%). Envelope de degradacao sistemica
        # (+20%, observado em suites sob carga) => pior caso realista ~153s.
        # Teto final 180s: ~1.4x o pior caso realista -- detecta hang real
        # mais rapido que 240s e mantem folga sobre o ruido medido. A
        # propriedade-guarda (ResourceWarning vira erro => rc != 0) e
        # binaria e independe do teto: vazamento de conexao e pego em
        # qualquer duracao.
        #
        # FECHAMENTO 1/6 (a guarda acima NAO era binaria de fato — medido):
        # no Python 3.12 `sqlite3` nao emite ResourceWarning para conexao
        # nao fechada (so a partir do 3.13), e ResourceWarning nascido em
        # destrutor vira "Exception ignored" com rc=0 mesmo sob `-W error`.
        # Prova por mutacao (conexao nunca fechada injetada em `init_db`):
        # o comando antigo terminou rc=0 (2/2 — cego); o runner estrito
        # `tests/_helpers/strict_resource_runner.py` terminou rc=2 (2/2).
        # O teto (180s) so limita a espera por um HANG — nunca foi o
        # detector de vazamento. Medicoes do subconjunto: Windows/4 CPUs
        # (relatorio da sessao anterior) 125.9-127.8s; Linux/1 CPU
        # (fechamento 1/6) 2.1-2.3s. 180s cobre o pior ambiente medido.
        import io
        import subprocess
        import sys

        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        result = subprocess.run(
            [
                sys.executable, "-W", "error::ResourceWarning",
                os.path.join(project_root, "tests", "_helpers", "strict_resource_runner.py"),
                "tests.test_migration_v6_recovery", "tests.test_migration_atomicity",
                "tests.test_atomic_idempotency", "tests.test_commit_failure_handling",
                "tests.test_cross_domain_idempotency",
            ],
            cwd=project_root, capture_output=True, text=True, timeout=180,
        )
        self.assertEqual(
            result.returncode, 0,
            f"testes de migração falharam, vazaram conexão SQLite (rc=2) ou geraram "
            f"ResourceWarning/exceção ignorada em destrutor (rc=3):\n"
            f"STDOUT:{result.stdout}\nSTDERR:{result.stderr}",
        )


if __name__ == "__main__":
    unittest.main()
