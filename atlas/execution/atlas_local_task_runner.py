"""Adapter between ATLAS PlanTask and controlled local execution.

No natural-language command generation.
A task must explicitly request:
- tool: local_readonly
- resource: action:<allowlisted action>
- optional resource: target:<approved local path>

The underlying LocalCommandExecutor remains the security boundary.
"""

from __future__ import annotations

from typing import Any

from .local_command_executor import (
    LocalCommandExecutor,
    LocalExecutionPolicyViolation,
)


class AtlasLocalTaskRunner:
    TOOL_NAME = "local_readonly"

    def __init__(
        self,
        executor: LocalCommandExecutor | None = None,
    ) -> None:
        self.executor = executor or LocalCommandExecutor()

    @staticmethod
    def _parse_resources(task: Any) -> tuple[str, str | None]:
        resources = list(getattr(task, "resources", None) or [])

        action = None
        target = None

        for item in resources:
            if not isinstance(item, str):
                continue

            if item.startswith("action:"):
                action = item.split(":", 1)[1].strip()

            elif item.startswith("target:"):
                target = item.split(":", 1)[1].strip()

        if not action:
            raise LocalExecutionPolicyViolation(
                "ATLAS local task requires explicit action:<name>"
            )

        return action, target

    def __call__(self, task: Any) -> dict:
        tools = list(getattr(task, "tools", None) or [])

        if self.TOOL_NAME not in tools:
            raise LocalExecutionPolicyViolation(
                "task not authorized for local_readonly execution"
            )

        action, target = self._parse_resources(task)

        result = self.executor.execute_readonly(
            action,
            target=target,
        )

        return {
            "executor": "ATLAS_LOCAL_EXECUTOR",
            "task_id": getattr(task, "task_id", None),
            "action": action,
            "target": target,
            "ok": result.ok,
            "returncode": result.returncode,
            "stdout": result.stdout[:32768],
            "stderr": result.stderr[:32768],
            "duration_seconds": result.duration_seconds,
            "timed_out": result.timed_out,
        }
