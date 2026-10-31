import os
import tempfile
import time
import unittest

from bridge.execution.vault import (
    SecretDecryptionFailed, SecretNotFound, TokenType, TokenVault, VaultKeyMode,
)
from core.models import db as db_module

"""
Testes BLOCO FINAL 2 (item 1 — cofre seguro de tokens/segredos):
criptografia real, revogação real, renovação por expiração,
persistência através de restart com a mesma chave, falha honesta com
chave errada.
"""


class TokenVaultInMemoryTests(unittest.TestCase):
    def test_store_and_retrieve_real_value(self):
        vault = TokenVault()
        ref = vault.store_token(platform="youtube", token_type=TokenType.API_KEY, value="segredo123")
        self.assertEqual(vault.retrieve_token(ref), "segredo123")

    def test_encrypted_value_never_plaintext(self):
        vault = TokenVault()
        ref = vault.store_token(platform="youtube", token_type=TokenType.API_KEY, value="segredo_super_secreto")
        encrypted = vault._encrypted_values[ref]
        self.assertNotIn(b"segredo_super_secreto", encrypted)

    def test_revoked_token_never_retrievable(self):
        vault = TokenVault()
        ref = vault.store_token(platform="x", token_type=TokenType.API_KEY, value="x")
        vault.revoke(ref)
        with self.assertRaises(SecretNotFound):
            vault.retrieve_token(ref)

    def test_unknown_reference_raises(self):
        vault = TokenVault()
        with self.assertRaises(SecretNotFound):
            vault.retrieve_token("nao_existe")

    def test_needs_renewal_true_within_safety_margin(self):
        vault = TokenVault()
        ref = vault.store_token(
            platform="x", token_type=TokenType.OAUTH2_ACCESS_TOKEN, value="x", expires_at=time.time() + 60,
        )
        self.assertTrue(vault.needs_renewal(ref, safety_margin_seconds=300))

    def test_needs_renewal_false_when_far_from_expiry(self):
        vault = TokenVault()
        ref = vault.store_token(
            platform="x", token_type=TokenType.OAUTH2_ACCESS_TOKEN, value="x", expires_at=time.time() + 10000,
        )
        self.assertFalse(vault.needs_renewal(ref, safety_margin_seconds=300))

    def test_no_expiry_never_needs_renewal(self):
        vault = TokenVault()
        ref = vault.store_token(platform="x", token_type=TokenType.API_KEY, value="x", expires_at=None)
        self.assertFalse(vault.needs_renewal(ref))

    def test_list_by_platform_excludes_revoked(self):
        vault = TokenVault()
        ref1 = vault.store_token(platform="youtube", token_type=TokenType.API_KEY, value="a")
        ref2 = vault.store_token(platform="youtube", token_type=TokenType.API_KEY, value="b")
        vault.revoke(ref2)
        active = vault.list_by_platform("youtube")
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0].reference_id, ref1)


class TokenVaultPersistenceTests(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db_module.remove_db_file(self.db_path)
        db_module.init_db(self.db_path)

    def tearDown(self):
        db_module.remove_db_file(self.db_path)

    def test_ephemeral_mode_without_master_key(self):
        vault = TokenVault(db_path=self.db_path, master_key=None)
        self.assertEqual(vault.key_mode, VaultKeyMode.EPHEMERAL)

    def test_persistent_mode_survives_restart_with_same_key(self):
        vault1 = TokenVault(db_path=self.db_path, master_key="chave-mestra-teste")
        ref = vault1.store_token(platform="hotmart", token_type=TokenType.API_KEY, value="valor_real")

        vault2 = TokenVault(db_path=self.db_path, master_key="chave-mestra-teste")
        self.assertEqual(vault2.retrieve_token(ref), "valor_real")

    def test_wrong_master_key_fails_honestly_never_fake_success(self):
        vault1 = TokenVault(db_path=self.db_path, master_key="chave-correta")
        ref = vault1.store_token(platform="x", token_type=TokenType.API_KEY, value="x")

        vault2 = TokenVault(db_path=self.db_path, master_key="chave-errada")
        with self.assertRaises(SecretDecryptionFailed):
            vault2.retrieve_token(ref)

    def test_revocation_persists_across_restart(self):
        vault1 = TokenVault(db_path=self.db_path, master_key="k")
        ref = vault1.store_token(platform="x", token_type=TokenType.API_KEY, value="x")
        vault1.revoke(ref)

        vault2 = TokenVault(db_path=self.db_path, master_key="k")
        with self.assertRaises(SecretNotFound):
            vault2.retrieve_token(ref)


if __name__ == "__main__":
    unittest.main()
