"""
Runner estrito de recursos para `test_migration_tests_produce_no_resource_warnings`.

FECHAMENTO 1/6 — por que este runner existe (achado medido):
`python -W error::ResourceWarning -m unittest ...` NAO detecta vazamento de
conexao SQLite no Python 3.12:
  1. `sqlite3` so passou a emitir ResourceWarning para conexao nao fechada no
     Python 3.13; no 3.12 nenhum aviso e emitido (0 avisos medidos).
  2. Mesmo para recursos que emitem (ex.: arquivo), o aviso nasce no destrutor;
     sob `-W error` vira "Exception ignored" e o processo termina com rc=0.
Prova: modulo com conexao vazada e modulo com arquivo vazado => ambos rc=0.

Este runner:
  - rastreia toda conexao criada por `sqlite3.connect` com a fabrica padrao
    (caminho de producao) e registra as que foram coletadas sem `close()`;
  - instala `sys.unraisablehook` para que excecoes "ignoradas" em destrutor
    (ResourceWarning promovido a erro, etc.) sejam registradas;
  - ao final: `gc.collect()` e sai com rc != 0 se houver qualquer registro.
So stdlib. Nao altera producao.
"""
import gc
import os
import sqlite3
import sys
import traceback
import unittest
import warnings

_leaks: list = []
_unraisable: list = []
_orig_connect = sqlite3.connect
_tracked_classes: dict = {}


def _tracked_class(base):
    cls = _tracked_classes.get(base)
    if cls is not None:
        return cls

    class _Tracked(base):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self._mgs_closed = False
            self._mgs_origin = "".join(traceback.format_list(traceback.extract_stack(limit=8)[:-2]))

        def close(self):
            self._mgs_closed = True
            return super().close()

        def __del__(self):
            if not getattr(self, "_mgs_closed", True):
                _leaks.append(getattr(self, "_mgs_origin", "<origem desconhecida>"))

    _Tracked.__name__ = "Tracked" + base.__name__
    _tracked_classes[base] = _Tracked
    return _Tracked


def _tracked_connect(*args, **kwargs):
    # Apenas a fabrica PADRAO e rastreada: e o caminho de producao
    # (`core.models.db` nunca passa `factory=`). Fabricas proprias de
    # testes (injecao de falha com estado de CLASSE, ex.
    # `type(self).should_fail`) seriam alteradas semanticamente por uma
    # subclasse — ficam intocadas.
    if kwargs.get("factory") not in (None, sqlite3.Connection):
        return _orig_connect(*args, **kwargs)
    kwargs["factory"] = _tracked_class(sqlite3.Connection)
    return _orig_connect(*args, **kwargs)


def _hook(unraisable):
    _unraisable.append(
        "%s: %s (objeto: %r)" % (
            type(unraisable.exc_value).__name__, unraisable.exc_value, unraisable.object,
        )
    )


_orig_showwarning = warnings.showwarning


def _showwarning(message, category, filename, lineno, file=None, line=None):
    # ResourceWarning que escapar do filtro "error" (ex.: filtro reescrito
    # por algum teste) ainda e REGISTRADO, nunca apenas impresso.
    if issubclass(category, ResourceWarning):
        _unraisable.append("ResourceWarning: %s (%s:%s)" % (message, filename, lineno))
    return _orig_showwarning(message, category, filename, lineno, file, line)


def main(argv: list) -> int:
    # `unittest.main` reescreve os filtros para "default" quando
    # `sys.warnoptions` esta vazio — declarar a opcao impede isso.
    if "error::ResourceWarning" not in sys.warnoptions:
        sys.warnoptions.append("error::ResourceWarning")
    warnings.simplefilter("error", ResourceWarning)
    warnings.showwarning = _showwarning
    sys.unraisablehook = _hook
    if os.getcwd() not in sys.path:
        sys.path.insert(0, os.getcwd())
    sqlite3.connect = _tracked_connect
    program = unittest.main(module=None, argv=["strict_resource_runner"] + argv, exit=False)
    gc.collect()
    ok = program.result.wasSuccessful()
    if _leaks:
        sys.stderr.write("\nVAZAMENTO: %d conexao(oes) SQLite coletada(s) sem close():\n" % len(_leaks))
        for origin in _leaks[:10]:
            sys.stderr.write("--- criada em:\n%s\n" % origin)
    if _unraisable:
        sys.stderr.write("\nEXCECAO(OES) IGNORADA(S) EM DESTRUTOR: %d\n" % len(_unraisable))
        for item in _unraisable[:10]:
            sys.stderr.write("  %s\n" % item)
    if not ok:
        return 1
    if _leaks:
        return 2
    if _unraisable:
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
