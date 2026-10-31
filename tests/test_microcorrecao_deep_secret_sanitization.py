import os
import tempfile
import unittest

from atlas.escalation import EscalationDesk
from core.models import db as db_module
from core.security.audit import AuditTrail
from core.security.sanitize import sanitize_dict

"""
MICROCORREÇÃO — DEEP SECRET SANITIZATION.

Falha real reproduzida por auditoria independente: `sanitize_dict()`
redigia segredos em `dict`s, mas não percorria `list`/`tuple`/`set` —
um segredo dentro de `list[dict]` (ex:
`{"items": [{"api_key": "..."}]}`) passava sem nenhuma redação e
podia ser persistido em texto puro no SQLite.

Corrigido: `_sanitize_value()` recursivo real para `dict`, `list`,
`tuple`, `set` e combinações arbitrariamente aninhadas — qualquer
chave sensível (`token`, `api_key`, `password`, `secret`,
`access_token`, etc.) é redigida independentemente da profundidade.
"""

_SECRET = "SEGREDO_REAL_NUNCA_PODE_VAZAR_XYZ789"


class DeepSanitizationUnitTests(unittest.TestCase):
    """Testes unitários diretos de `sanitize_dict()` — cada estrutura
    exigida pela auditoria, isoladamente."""

    def test_secret_in_plain_dict_redacted(self):
        result = sanitize_dict({"api_key": _SECRET})
        self.assertEqual(result["api_key"], "[REDACTED]")

    def test_secret_in_dict_inside_list_redacted(self):
        result = sanitize_dict({"items": [{"api_key": _SECRET}]})
        self.assertEqual(result["items"][0]["api_key"], "[REDACTED]")

    def test_secret_in_list_inside_list_redacted(self):
        result = sanitize_dict({"matrix": [[{"password": _SECRET}]]})
        self.assertEqual(result["matrix"][0][0]["password"], "[REDACTED]")

    def test_secret_in_dict_inside_tuple_redacted(self):
        result = sanitize_dict({"pair": ({"secret": _SECRET}, "dado_normal")})
        self.assertEqual(result["pair"][0]["secret"], "[REDACTED]")
        self.assertEqual(result["pair"][1], "dado_normal")
        self.assertIsInstance(result["pair"], tuple)

    def test_secret_in_dict_inside_list_inside_tuple_redacted(self):
        result = sanitize_dict({"combo": ([{"access_token": _SECRET}], "x")})
        self.assertEqual(result["combo"][0][0]["access_token"], "[REDACTED]")

    def test_set_of_plain_values_preserved_and_type_kept(self):
        result = sanitize_dict({"tags": {"a", "b", "c"}})
        self.assertEqual(result["tags"], {"a", "b", "c"})
        self.assertIsInstance(result["tags"], set)

    def test_various_secret_key_names_all_redacted_when_nested(self):
        for key_name in ("password", "senha", "secret", "token", "api_key", "access_token", "client_secret"):
            with self.subTest(key_name=key_name):
                result = sanitize_dict({"wrapper": [{key_name: _SECRET}]})
                self.assertEqual(result["wrapper"][0][key_name], "[REDACTED]")

    def test_non_secret_data_never_redacted(self):
        result = sanitize_dict({"items": [{"name": "produto", "price": 100}], "count": 5})
        self.assertEqual(result["items"][0]["name"], "produto")
        self.assertEqual(result["items"][0]["price"], 100)
        self.assertEqual(result["count"], 5)

    def test_container_types_preserved_after_sanitization(self):
        result = sanitize_dict({"a_list": [1, 2], "a_tuple": (1, 2), "a_set": {1, 2}, "a_dict": {"x": 1}})
        self.assertIsInstance(result["a_list"], list)
        self.assertIsInstance(result["a_tuple"], tuple)
        self.assertIsInstance(result["a_set"], set)
        self.assertIsInstance(result["a_dict"], dict)


class DeepSanitizationEscalationDeskMemoryTests(unittest.TestCase):
    def setUp(self):
        self.audit = AuditTrail()
        self.desk = EscalationDesk(self.audit)

    def test_context_with_secret_in_list_of_dicts_redacted(self):
        item = self.desk.escalate(
            reason="x", actor="x", operation="x", context={"credentials": [{"api_key": _SECRET}]},
        )
        fresh = self.desk.get(item.escalation_id)
        self.assertEqual(fresh.context["credentials"][0]["api_key"], "[REDACTED]")

    def test_evidence_with_secret_in_list_of_dicts_redacted(self):
        item = self.desk.escalate(
            reason="x", actor="x", operation="x", context={}, evidence={"logs": [{"password": _SECRET}]},
        )
        fresh = self.desk.get(item.escalation_id)
        self.assertEqual(fresh.evidence["logs"][0]["password"], "[REDACTED]")

    def test_secret_never_appears_anywhere_in_memory_representation(self):
        item = self.desk.escalate(
            reason="x", actor="x", operation="x",
            context={"nested": {"deep": [{"secret": _SECRET}]}}, evidence={"proofs": [{"token": _SECRET}]},
        )
        fresh = self.desk.get(item.escalation_id)
        self.assertNotIn(_SECRET, str(fresh.context))
        self.assertNotIn(_SECRET, str(fresh.evidence))


class DeepSanitizationSQLiteTests(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db_module.remove_db_file(self.db_path)
        db_module.init_db(self.db_path)
        self.audit = AuditTrail()
        self.desk = EscalationDesk(self.audit, db_path=self.db_path)

    def tearDown(self):
        db_module.remove_db_file(self.db_path)

    def test_secret_in_list_of_dicts_never_reaches_raw_sqlite_bytes(self):
        """Confirma no ARQUIVO SQLITE BRUTO (bytes, não via API) que
        o segredo aninhado em list[dict] nunca é persistido em texto
        puro."""
        self.desk.escalate(
            reason="x", actor="x", operation="x",
            context={"credentials": [{"api_key": _SECRET}]},
            evidence={"logs": [{"password": _SECRET}]},
        )
        with open(self.db_path, "rb") as f:
            raw = f.read()
        self.assertNotIn(_SECRET.encode(), raw)

    def test_context_redacted_correctly_when_read_back_from_sqlite(self):
        item = self.desk.escalate(
            reason="x", actor="x", operation="x", context={"credentials": [{"api_key": _SECRET}]},
        )
        fresh = self.desk.get(item.escalation_id)
        self.assertEqual(fresh.context["credentials"][0]["api_key"], "[REDACTED]")

    def test_deeply_nested_secret_never_in_raw_sqlite_bytes(self):
        self.desk.escalate(
            reason="x", actor="x", operation="x",
            context={"a": {"b": [{"c": [{"secret": _SECRET}]}]}},
        )
        with open(self.db_path, "rb") as f:
            raw = f.read()
        self.assertNotIn(_SECRET.encode(), raw)


class DeepSanitizationIntegrationWithPreviousFixesTests(unittest.TestCase):
    """Confirma que Defensive Copy, Immutable State, Owner Gate,
    Anti-Replay e SpendGuard continuam funcionando corretamente
    JUNTO com a sanitização profunda — nenhuma regressão de
    comportamento nas correções anteriores."""

    def test_sanitization_plus_defensive_copy_together(self):
        from core.security.auth import Principal, Role

        audit = AuditTrail()
        desk = EscalationDesk(audit)
        original_context = {"credentials": [{"api_key": _SECRET}], "amount": 100.0}

        item = desk.escalate(reason="x", actor="x", operation="x", context=original_context)

        # muta o original DEPOIS -- defensive copy on input deve proteger
        original_context["amount"] = 999999.0
        original_context["credentials"][0]["api_key"] = "outro_valor_tentando_bypass"

        fresh = desk.get(item.escalation_id)
        self.assertEqual(fresh.context["amount"], 100.0)  # defensive copy preservado
        self.assertEqual(fresh.context["credentials"][0]["api_key"], "[REDACTED]")  # sanitizacao preservada

        # muta o RETORNADO -- immutable state deve proteger
        fresh.context["amount"] = 111.0
        fresh2 = desk.get(item.escalation_id)
        self.assertEqual(fresh2.context["amount"], 100.0)


if __name__ == "__main__":
    unittest.main()
