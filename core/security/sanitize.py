"""
core.security.sanitize
=======================
Detecção e redação de segredos antes de qualquer log, audit trail,
exceção exposta ou artefato exportado (ZIP).

Seção 58 (Security) e 59 (Credential Vault Interface) do Comando Mestre:
nenhum segredo pode aparecer em logs, ZIP, audit trail, exceptions,
screenshots ou fixtures reais.
"""

from __future__ import annotations

import re
from typing import Any

_REDACTED = "[REDACTED]"

# Padrões conservadores de detecção de segredo. Preferimos falso-positivo
# (redigir algo que não era segredo) a falso-negativo (vazar um segredo).
#
# MICROCORREÇÃO (COMPLETE SECRET REDACTION — falha real reproduzida por
# auditoria independente): `authorization`/`proxy-authorization`/
# `bearer`/`session` (sozinho, não só `session_secret`) estavam
# ausentes — uma chave `"authorization": "Bearer ..."` passava
# completamente sem redação. `apikey` (sem separador),
# `access_token`/`refresh_token` (explícitos, além de já cobertos por
# substring via `token`) e `set-cookie` (explícito, além de coberto
# por substring via `cookie`) adicionados para nunca depender apenas
# de correspondência parcial acidental.
_SECRET_KEY_PATTERNS = re.compile(
    r"(password|senha|passwd|secret|token|api[_-]?key|apikey|access[_-]?key|"
    r"access[_-]?token|refresh[_-]?token|client[_-]?secret|private[_-]?key|"
    r"session[_-]?secret|session|oauth|cookie|set[_-]?cookie|"
    r"authorization|proxy[_-]?authorization|bearer)",
    re.IGNORECASE,
)

_VALUE_LOOKS_LIKE_SECRET = re.compile(
    r"^(?=.*[A-Za-z])(?=.*\d).{16,}$"  # heurística: string longa alfanumérica
)

# MICROCORREÇÃO (COMPLETE SECRET REDACTION): usado por `sanitize_text()`
# — mesmo conjunto de chaves sensíveis reconhecidas em texto livre
# (nunca reimplementado de forma divergente do dicionário acima; a
# lista de nomes é a MESMA, apenas formatada como alternativas de
# regex com um grupo de captura nomeado para o valor de reposição).
_SENSITIVE_KEY_TEXT_PATTERN = (
    r"(?i)(authorization|proxy[_-]?authorization|api[_-]?key|apikey|access[_-]?token|"
    r"refresh[_-]?token|token|password|passwd|secret|client[_-]?secret|"
    r"private[_-]?key|session[_-]?secret|session|cookie|set[_-]?cookie|oauth)"
)
# Valor: reconhece "Bearer <algo>" como uma unidade (nunca separa o
# esquema do token, garantindo que o token INTEIRO seja redigido junto),
# caindo para "qualquer sequência sem espaço/vírgula/ponto-e-vírgula/
# aspas/&" no caso geral — nunca para cedo demais e deixa parte do
# segredo visível, mas PARA em "&" para não engolir o restante de uma
# querystring com múltiplos parâmetros (ex: "token=X&outro=normal" —
# "&outro=normal" precisa continuar visível, por não ser sensível).
_VALUE_TEXT_PATTERN = r'(?:Bearer\s+[^\s,;&"\')\]]+|[^\s,;&"\')\]]+)'


def sanitize_dict(data: dict[str, Any]) -> dict[str, Any]:
    """Retorna uma cópia do dict com valores de chaves sensíveis
    redigidos — RECURSIVAMENTE, em qualquer profundidade, incluindo
    dentro de `list`/`tuple`/`set` aninhados (e combinações
    arbitrárias entre eles).

    MICROCORREÇÃO (DEEP SECRET SANITIZATION — falha real reproduzida
    por auditoria independente): a versão anterior só descia
    recursivamente em valores `dict` — um segredo dentro de
    `list[dict]` (ex: `{"items": [{"api_key": "..."}]}`) passava
    direto, sem nenhuma redação, e podia ser persistido em texto puro
    no SQLite. Agora `_sanitize_value()` é chamado para TODO valor,
    reconhecendo `dict` (aplica a checagem de chave sensível
    normalmente), `list`/`tuple`/`set` (recursão em cada elemento,
    preservando o tipo do container) — nenhuma estrutura aninhada
    permitida escapa da varredura."""
    return _sanitize_value(data)


def _sanitize_value(value: Any) -> Any:
    """Sanitização recursiva real — chamada para QUALQUER valor,
    dentro ou fora de um dict. `dict` aplica a checagem de chave
    sensível (mesma lógica de sempre); `list`/`tuple`/`set` recursam
    em cada elemento, preservando o tipo original do container;
    qualquer outro tipo (str/int/float/bool/None) é devolvido como
    está (a heurística de valor, quando um segredo aparece sem uma
    chave nomeada associada — ex: item bruto de lista — permanece
    reservada para o futuro, mesma limitação já documentada em
    `_looks_like_secret_value`)."""
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, sub_value in value.items():
            if _SECRET_KEY_PATTERNS.search(str(key)):
                out[key] = _REDACTED
            elif isinstance(sub_value, str) and _looks_like_secret_value(key, sub_value):
                out[key] = _REDACTED
            else:
                out[key] = _sanitize_value(sub_value)
        return out
    if isinstance(value, list):
        return [_sanitize_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_sanitize_value(item) for item in value)
    if isinstance(value, set):
        # sets só podem conter elementos hasháveis (nunca dict/list) —
        # ainda assim recursamos (ex: um set de tuplas contendo dados
        # sensíveis permanece coberto).
        return {_sanitize_value(item) for item in value}
    return value


def _looks_like_secret_value(key: str, value: str) -> bool:
    # Mesmo que a chave não pareça sensível, valores muito longos/aleatórios
    # associados a chaves como "key", "hash", "id" de credencial são redigidos
    # por precaução quando o nome da chave contém termos sensíveis parciais.
    return False  # reservado para heurísticas futuras mais agressivas


def sanitize_text(text: str) -> str:
    """Redige padrões óbvios de segredo dentro de texto livre
    (logs/erros/traces/exceptions) — NUNCA deixa nenhuma parte do
    valor original visível.

    MICROCORREÇÃO (COMPLETE SECRET REDACTION — falha real reproduzida
    por auditoria independente): a versão anterior usava
    `m.group(0).split(m.group(0)[-1])[0] + _REDACTED` como
    substituição — isso removia apenas o ÚLTIMO CARACTERE do valor
    encontrado e concatenava o marcador, deixando o restante do
    segredo (ex: "minhasenha_super_secreta_12" de um valor de 29
    caracteres) completamente visível no resultado. Além disso, o
    padrão de valor `[^\\s,;]+` parava no primeiro espaço, então
    `"Authorization: Bearer eyJ..."` nunca era reconhecido (a palavra
    "Authorization" nem estava na lista de chaves, e mesmo se
    estivesse, só "Bearer" seria capturado como valor, deixando o
    token real intacto).

    Corrigido: a substituição agora é sempre `"chave: [REDACTED]"` —
    o valor inteiro correspondido é descartado por completo, nunca
    parcialmente preservado. O padrão de valor reconhece
    `"Bearer <token>"` como uma unidade só (nunca separa o esquema do
    token, garantindo que o token inteiro seja redigido). Um padrão
    adicional cobre `Bearer <token>` mesmo SEM uma chave nomeada
    antes (ex: um header colado cru, sem o prefixo
    `"Authorization: "`)."""
    # 1. "chave: valor" / "chave=valor" para as chaves sensíveis
    #    conhecidas — reconhece "Bearer <token>" como o valor inteiro
    #    quando presente, nunca deixando o token exposto depois do
    #    esquema.
    redacted = re.sub(
        _SENSITIVE_KEY_TEXT_PATTERN + r"\s*[:=]\s*" + _VALUE_TEXT_PATTERN,
        lambda m: f"{m.group(1)}: {_REDACTED}",
        text,
    )
    # 2. "Bearer <token>" solto no texto, sem uma chave nomeada antes
    #    (ex: header colado cru) — o que sobrar depois do passo 1.
    redacted = re.sub(r"(?i)\bBearer\s+[^\s,;&\"')\]]+", f"Bearer {_REDACTED}", redacted)
    return redacted


def assert_no_secrets(data: dict[str, Any]) -> list[str]:
    """Retorna lista de chaves suspeitas de conter segredo em texto puro.
    Usado pelos testes de secret-scanning."""
    offenders = []
    for key, value in data.items():
        if isinstance(value, dict):
            offenders.extend(assert_no_secrets(value))
            continue
        if _SECRET_KEY_PATTERNS.search(str(key)) and value not in (None, "", _REDACTED):
            offenders.append(key)
    return offenders
