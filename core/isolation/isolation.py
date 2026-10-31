"""
core.isolation.isolation
=========================
Project Isolation e Channel Isolation obrigatórios (Seções 6, 55, 58).

Nunca misturar entre canais/projetos: DNA, memória, métricas, produto,
funil, estratégia, aprendizado, credencial.

A estratégia aqui é simples e explícita: todo acesso a um recurso passa
por `IsolationGuard`, que valida se o `channel_id`/`project_id` do
principal (escopo) bate com o do recurso. Nada de checagem implícita.
"""

from __future__ import annotations

from dataclasses import dataclass


class IsolationViolation(Exception):
    pass


@dataclass
class ResourceScope:
    channel_id: str | None = None
    project_id: str | None = None


class IsolationGuard:
    """Valida que um principal só acessa recursos dentro do seu escopo.

    scopes no formato {"channel:<id>", "project:<id>"} ou {"*"} para Owner.
    """

    @staticmethod
    def check(principal_scopes: set[str], resource: ResourceScope) -> None:
        if "*" in principal_scopes:
            return  # Owner / superescopo explícito

        if resource.channel_id is not None:
            if f"channel:{resource.channel_id}" not in principal_scopes:
                raise IsolationViolation(
                    f"acesso negado: fora do escopo do canal {resource.channel_id}"
                )

        if resource.project_id is not None:
            if f"project:{resource.project_id}" not in principal_scopes:
                raise IsolationViolation(
                    f"acesso negado: fora do escopo do projeto {resource.project_id}"
                )

    @staticmethod
    def assert_same_channel(a_channel_id: str, b_channel_id: str) -> None:
        if a_channel_id != b_channel_id:
            raise IsolationViolation(
                f"violação de isolamento entre canais: {a_channel_id} != {b_channel_id}"
            )

    @staticmethod
    def assert_same_project(a_project_id: str, b_project_id: str) -> None:
        if a_project_id != b_project_id:
            raise IsolationViolation(
                f"violação de isolamento entre projetos: {a_project_id} != {b_project_id}"
            )
