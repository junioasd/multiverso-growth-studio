"""
forge.preparation
===================
COMANDO 3/6 — FORGE: preparação e validação de artefatos de EXECUÇÃO.

O FORGE existente (`forge.engineer`) é o pipeline de REPARO. Faltava a
outra metade exigida: transformar um passo aprovado do plano do NAVIGATOR
em um artefato executável VALIDADO, e validar a saída depois.

Regras estruturais:
- nunca inventa: campo obrigatório ausente/vazio => artefato BLOQUEADO
  com a lista EXATA do que falta;
- credenciais nunca entram no payload: chaves com cara de segredo são
  recusadas — o token real só é lido do `TokenVault` pelo adapter, no
  momento do envio;
- o payload é congelado com um hash (`payload_hash`): o artefato que
  executa é exatamente o que foi planejado/aprovado (a aprovação do
  Owner ainda é vinculada pelo `NavigatorReal` ao fingerprint dos
  parâmetros — duas travas independentes);
- a saída é validada contra critérios objetivos do passo.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field

from core.governance.idempotency import stable_fingerprint

_SECRET_KEY_MARKERS = ("token", "password", "senha", "secret", "api_key", "apikey", "authorization",
                       "bearer", "client_secret", "private_key", "cookie")


@dataclass(frozen=True)
class ExecutionArtifact:
    step_id: str
    platform: str
    action: str
    adapter_method: str
    params: dict
    payload_hash: str


@dataclass
class PreparationResult:
    ready: bool
    artifact: ExecutionArtifact | None = None
    missing: list = field(default_factory=list)
    violations: list = field(default_factory=list)

    def reason(self) -> str:
        parts = []
        if self.missing:
            parts.append("faltam campos obrigatórios: " + ", ".join(self.missing))
        if self.violations:
            parts.append("violações: " + "; ".join(self.violations))
        return " | ".join(parts)


def _find_secret_keys(value, path="") -> list:
    found = []
    if isinstance(value, dict):
        for k, v in value.items():
            p = f"{path}.{k}" if path else str(k)
            if any(m in str(k).lower() for m in _SECRET_KEY_MARKERS):
                found.append(p)
            found.extend(_find_secret_keys(v, p))
    elif isinstance(value, (list, tuple)):
        for i, v in enumerate(value):
            found.extend(_find_secret_keys(v, f"{path}[{i}]"))
    return found


def payload_hash(*, platform: str, action: str, adapter_method: str, params: dict) -> str:
    return stable_fingerprint("forge_artifact", platform, action, adapter_method, params)


class ExecutionPreparer:
    """Prepara e valida artefatos. Sem estado — determinístico."""

    def prepare(self, *, step_id: str, platform: str, action: str, adapter_method: str | None, params: dict,
                required_params: list | tuple = (), expected_hash: str | None = None) -> PreparationResult:
        missing = [k for k in required_params
                   if k not in params or params[k] is None or (isinstance(params[k], str) and not params[k].strip())]
        if not adapter_method:
            missing.append("adapter_method (chamada da API oficial)")
        violations = [f"credencial no payload ({p}) — use o TokenVault" for p in _find_secret_keys(params)]
        if missing or violations:
            return PreparationResult(ready=False, missing=missing, violations=violations)
        frozen = copy.deepcopy(params)
        digest = payload_hash(platform=platform, action=action, adapter_method=adapter_method, params=frozen)
        if expected_hash is not None and digest != expected_hash:
            return PreparationResult(ready=False, violations=[
                "payload ALTERADO após o planejamento/aprovação (hash divergente) — execução recusada"])
        return PreparationResult(ready=True, artifact=ExecutionArtifact(
            step_id=step_id, platform=platform, action=action, adapter_method=adapter_method, params=frozen,
            payload_hash=digest))

    @staticmethod
    def validate_output(*, data: dict, required_result_keys: list | tuple = ()) -> list:
        """Critério objetivo de saída: devolve a lista do que falta no
        resultado (vazia = válido). Nunca presume sucesso."""
        data = data or {}
        return [k for k in required_result_keys if k not in data or data[k] in (None, "")]
