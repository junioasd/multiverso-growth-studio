"""
tests._auth_test_helpers
============================
CORREÇÃO (itens 24-26 — regressão monolítica: reduzir dependência de
wall-clock nos testes). `hash_password()`/`verify_password()` reais
usam PBKDF2 com 260.000 iterações (valor OWASP, correto para
produção) — cada chamada custa dezenas de milissegundos, cujo custo
escala diretamente com a velocidade da CPU. Testes de FLUXO de
autenticação (login/sessão/RBAC) não dependem da força criptográfica
real — usam senhas fixas de fixture, nunca credenciais reais — então
`fast_hash_password()` usa um número de iterações muito menor,
suficiente para exercitar o mesmo caminho de código
(`verify_password()` lê o número de iterações do próprio hash
armazenado, então continua funcionando normalmente com qualquer
valor). NUNCA usado para senhas reais de produção — só para fixtures
de teste."""

from core.security.auth import hash_password

# Baixo o bastante para ser rápido, alto o bastante para exercitar o
# mesmo código (nunca usado para segurança real).
_TEST_ITERATIONS = 1_000


def fast_hash_password(plain_password: str) -> str:
    return hash_password(plain_password, iterations=_TEST_ITERATIONS)
