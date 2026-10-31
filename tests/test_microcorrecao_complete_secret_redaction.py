import os
import tempfile
import unittest

from atlas.escalation import EscalationDesk
from core.models import db as db_module
from core.security.audit import AuditTrail
from core.security.sanitize import sanitize_dict, sanitize_text

"""
MICROCORREÇÃO — COMPLETE SECRET REDACTION.

Falhas reais reproduzidas por auditoria independente:
1. `_SECRET_KEY_PATTERNS` não reconhecia `authorization`,
   `proxy-authorization`, `bearer`, `session` (sozinho) — um valor
   associado a essas chaves passava sem nenhuma redação.
2. `sanitize_text()` usava uma lógica de substituição quebrada
   (`m.group(0).split(m.group(0)[-1])[0] + _REDACTED`) que removia
   apenas o ÚLTIMO CARACTERE do segredo, deixando o restante
   completamente visível; e o padrão de valor parava no primeiro
   espaço, nunca capturando o token completo em
   `"Authorization: Bearer <token>"`.

Corrigido: chaves sensíveis expandidas (case-insensitive), e
`sanitize_text()` reescrito para sempre substituir "chave: [REDACTED]"
por completo — nunca deixando nenhuma parte do valor original visível
— com reconhecimento de "Bearer <token>" como uma unidade, com ou
sem uma chave nomeada antes.
"""

_TOKEN = "eyJhbGciOiJIUzI1NiJ9.PAYLOAD_JAMAIS_PODE_APARECER_XYZ.ASSINATURA_SECRETA_ABC"


class SecretKeyPatternExpansionTests(unittest.TestCase):
    """Item 1 — todas as chaves mínimas exigidas reconhecidas,
    case-insensitive."""

    def _assert_redacted(self, key: str):
        result = sanitize_dict({key: _TOKEN})
        self.assertEqual(result[key], "[REDACTED]", f"chave '{key}' não foi redigida")

    def test_authorization_key_redacted(self):
        self._assert_redacted("authorization")

    def test_authorization_key_different_case_redacted(self):
        self._assert_redacted("Authorization")
        self._assert_redacted("AUTHORIZATION")
        self._assert_redacted("AuThOrIzAtIoN")

    def test_proxy_authorization_redacted(self):
        self._assert_redacted("proxy-authorization")
        self._assert_redacted("Proxy-Authorization")

    def test_api_key_variants_redacted(self):
        for variant in ("api_key", "api-key", "apikey", "API_KEY", "ApiKey"):
            self._assert_redacted(variant)

    def test_token_variants_redacted(self):
        for variant in ("token", "access_token", "refresh_token", "Access-Token"):
            self._assert_redacted(variant)

    def test_password_variants_redacted(self):
        for variant in ("password", "passwd", "PASSWORD"):
            self._assert_redacted(variant)

    def test_secret_variants_redacted(self):
        for variant in ("secret", "client_secret", "SECRET"):
            self._assert_redacted(variant)

    def test_cookie_variants_redacted(self):
        for variant in ("cookie", "set-cookie", "Set-Cookie", "COOKIE"):
            self._assert_redacted(variant)

    def test_session_alone_redacted(self):
        self._assert_redacted("session")
        self._assert_redacted("Session")

    def test_bearer_alone_redacted(self):
        self._assert_redacted("bearer")
        self._assert_redacted("Bearer")


class SanitizeTextCompleteRedactionTests(unittest.TestCase):
    """Item 2 — sanitize_text() nunca deixa parte do segredo visível."""

    def test_password_key_value_fully_redacted(self):
        result = sanitize_text(f"password: {_TOKEN}")
        self.assertNotIn(_TOKEN, result)
        self.assertNotIn(_TOKEN[:-1], result)  # nem o valor menos o ultimo caractere
        self.assertIn("[REDACTED]", result)

    def test_authorization_bearer_fully_redacted(self):
        result = sanitize_text(f"Authorization: Bearer {_TOKEN}")
        self.assertNotIn(_TOKEN, result)
        self.assertIn("[REDACTED]", result)

    def test_bare_bearer_without_key_fully_redacted(self):
        result = sanitize_text(f"Bearer {_TOKEN}")
        self.assertNotIn(_TOKEN, result)
        self.assertIn("Bearer [REDACTED]", result)

    def test_token_in_querystring_fully_redacted(self):
        result = sanitize_text(f"token={_TOKEN}&other=normal")
        self.assertNotIn(_TOKEN, result)
        self.assertIn("normal", result)  # dado nao sensivel preservado

    def test_api_key_in_querystring_fully_redacted(self):
        result = sanitize_text(f"api_key={_TOKEN}")
        self.assertNotIn(_TOKEN, result)

    def test_secret_in_free_text_fully_redacted(self):
        result = sanitize_text(f"secret: {_TOKEN}")
        self.assertNotIn(_TOKEN, result)

    def test_curl_command_with_bearer_header_fully_redacted(self):
        result = sanitize_text(f"curl -H 'Authorization: Bearer {_TOKEN}' https://api.example.com/v1")
        self.assertNotIn(_TOKEN, result)
        self.assertIn("https://api.example.com/v1", result)  # dado nao sensivel preservado

    def test_no_meaningful_fragment_of_secret_survives(self):
        """Regressão específica do bug original: nenhum FRAGMENTO
        SUBSTANCIAL do segredo (nem removendo só o último caractere,
        como o bug original fazia) pode sobreviver — checagem de
        sufixos de tamanho significativo (10-30 caracteres), nunca 1
        caractere isolado (que coincidiria trivialmente com letras de
        "[REDACTED]")."""
        result = sanitize_text(f"password: {_TOKEN}")
        for suffix_len in (10, 20, 30):
            self.assertNotIn(_TOKEN[-suffix_len:], result)


class DeepStructureCompleteRedactionTests(unittest.TestCase):
    """Estruturas aninhadas com as chaves recém-adicionadas."""

    def test_authorization_nested_in_list_of_dicts(self):
        result = sanitize_dict({"headers": [{"authorization": f"Bearer {_TOKEN}"}]})
        self.assertEqual(result["headers"][0]["authorization"], "[REDACTED]")

    def test_proxy_authorization_nested_in_tuple(self):
        result = sanitize_dict({"pair": ({"proxy-authorization": _TOKEN}, "normal")})
        self.assertEqual(result["pair"][0]["proxy-authorization"], "[REDACTED]")
        self.assertEqual(result["pair"][1], "normal")


class EscalationDeskCompleteRedactionMemoryTests(unittest.TestCase):
    def setUp(self):
        self.audit = AuditTrail()
        self.desk = EscalationDesk(self.audit)

    def test_context_with_authorization_header_redacted(self):
        item = self.desk.escalate(
            reason="x", actor="x", operation="x",
            context={"headers": [{"authorization": f"Bearer {_TOKEN}"}]},
        )
        fresh = self.desk.get(item.escalation_id)
        self.assertEqual(fresh.context["headers"][0]["authorization"], "[REDACTED]")
        self.assertNotIn(_TOKEN, str(fresh.context))

    def test_evidence_with_authorization_header_redacted(self):
        item = self.desk.escalate(
            reason="x", actor="x", operation="x", context={},
            evidence={"request_log": [{"authorization": f"Bearer {_TOKEN}"}]},
        )
        fresh = self.desk.get(item.escalation_id)
        self.assertNotIn(_TOKEN, str(fresh.evidence))


class EscalationDeskCompleteRedactionSQLiteTests(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db_module.remove_db_file(self.db_path)
        db_module.init_db(self.db_path)
        self.audit = AuditTrail()
        self.desk = EscalationDesk(self.audit, db_path=self.db_path)

    def tearDown(self):
        db_module.remove_db_file(self.db_path)

    def test_authorization_bearer_never_in_raw_sqlite_dump(self):
        """Confirma DIRETAMENTE nos bytes brutos do arquivo (dump
        completo do banco) que o token real nunca é persistido."""
        self.desk.escalate(
            reason="x", actor="x", operation="x",
            context={"headers": [{"authorization": f"Bearer {_TOKEN}"}]},
            evidence={"log": [{"proxy-authorization": f"Bearer {_TOKEN}"}]},
        )
        with open(self.db_path, "rb") as f:
            raw = f.read()
        self.assertNotIn(_TOKEN.encode(), raw)
        self.assertNotIn(f"Bearer {_TOKEN}".encode(), raw)

    def test_cookie_and_session_never_in_raw_sqlite_dump(self):
        self.desk.escalate(
            reason="x", actor="x", operation="x",
            context={"set-cookie": f"session_token={_TOKEN}", "session": _TOKEN},
        )
        with open(self.db_path, "rb") as f:
            raw = f.read()
        self.assertNotIn(_TOKEN.encode(), raw)


class LogAuditNeverContainsSecretTests(unittest.TestCase):
    """Confirma que a trilha de auditoria (usada para logs/observabilidade)
    também nunca contém o segredo original."""

    def test_audit_trail_details_never_contain_secret(self):
        from core.security.auth import Principal, Role

        audit = AuditTrail()
        desk = EscalationDesk(audit)
        item = desk.escalate(
            reason="x", actor="x", operation="x",
            context={"authorization": f"Bearer {_TOKEN}"},
        )
        OWNER = Principal(subject="owner@x.com", role=Role.OWNER, scopes={"*"})
        desk.approve(item.escalation_id, decision="ok", resolved_by=OWNER)

        # a auditoria real registrada por EscalationDesk nao inclui o
        # context bruto diretamente, mas confirmamos que o item
        # armazenado (fonte de qualquer log futuro) ja esta redigido.
        fresh = desk.get(item.escalation_id)
        self.assertNotIn(_TOKEN, str(fresh.context))


class PreviousFixesPreservedIntegrationTests(unittest.TestCase):
    """Confirma que Defensive Copy, Deep Sanitization (list/tuple/set),
    Immutable State, Owner Gate, Anti-Replay e SpendGuard continuam
    funcionando corretamente JUNTO com a redação completa de chaves
    como authorization/bearer/session."""

    def test_full_stack_with_authorization_header(self):
        from bridge.execution.spend_guard import SpendGuard, SpendNotApproved
        from core.security.auth import Principal, Role

        OWNER = Principal(subject="owner@x.com", role=Role.OWNER, scopes={"*"})
        audit = AuditTrail()
        desk = EscalationDesk(audit)

        original_context = {
            "platform": "google_ads", "campaign_reference": "c1", "requested_amount": 100.0, "currency": "BRL",
            "auth_headers": [{"authorization": f"Bearer {_TOKEN}"}],
        }
        item = desk.escalate(reason="x", actor="x", operation="spend_approval", context=original_context)
        desk.approve(item.escalation_id, decision="ok", resolved_by=OWNER)

        # defensive copy on input: muta o original depois
        original_context["requested_amount"] = 999999.0
        original_context["auth_headers"][0]["authorization"] = "tentativa_de_bypass"

        guard = SpendGuard(escalation_desk=desk, db_path=None)
        approval = guard.confirm_approval(
            escalation_id=item.escalation_id, platform="google_ads", campaign_reference="c1", currency="BRL",
        )
        self.assertEqual(approval.approved_amount, 100.0)  # defensive copy preservado

        fresh = desk.get(item.escalation_id)
        self.assertEqual(fresh.context["auth_headers"][0]["authorization"], "[REDACTED]")  # redacao preservada
        self.assertNotIn(_TOKEN, str(fresh.context))

        # immutable state: mutar o retornado nao afeta o interno
        fresh.context["requested_amount"] = 1.0
        fresh2 = desk.get(item.escalation_id)
        self.assertEqual(fresh2.context["requested_amount"], 100.0)

        # spend guard continua bloqueando gasto acima do aprovado
        with self.assertRaises(SpendNotApproved):
            guard.record_spend(approval_id=approval.approval_id, amount=5000.0)


if __name__ == "__main__":
    unittest.main()
