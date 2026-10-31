from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from atlas.execution.atlas_local_task_runner import AtlasLocalTaskRunner
from bridge.execution.platform_adapters import PlatformActionResult


class LocalAtlasAdapter:
    """Adapter governado entre NavigatorReal e AtlasLocalTaskRunner."""

    PLATFORM = "local"

    def __init__(self, runner: AtlasLocalTaskRunner | None = None) -> None:
        self.runner = runner or AtlasLocalTaskRunner()

    def execute_readonly(
        self,
        *,
        action: str,
        target: str | None = None,
        path: str | None = None,
        task_id: str | None = None,
    ) -> PlatformActionResult:
        if target is not None and path is not None and target != path:
            return PlatformActionResult(
                success=False,
                platform=self.PLATFORM,
                action=action,
                data={"executor": "ATLAS_LOCAL_EXECUTOR", "error_type": "ConflictingTarget"},
                reason="target and path refer to different resources",
                delivery="NOT_SENT",
            )

        target = target if target is not None else path
        resources = [f"action:{action}"]

        if target is not None:
            resources.append(f"target:{target}")

        task = SimpleNamespace(
            task_id=task_id or f"local-{uuid4().hex[:12]}",
            tools=[self.runner.TOOL_NAME],
            resources=resources,
        )

        try:
            result = self.runner(task)
        except Exception as exc:
            return PlatformActionResult(
                success=False,
                platform=self.PLATFORM,
                action=action,
                data={
                    "executor": "ATLAS_LOCAL_EXECUTOR",
                    "error_type": type(exc).__name__,
                },
                reason=f"{type(exc).__name__}: {exc}",
                delivery="NOT_SENT",
            )

        ok = bool(result.get("ok"))

        reason = ""
        if not ok:
            reason = (
                str(result.get("stderr") or "").strip()
                or f"local readonly execution failed; returncode={result.get('returncode')}"
            )

        return PlatformActionResult(
            success=ok,
            platform=self.PLATFORM,
            action=action,
            data=result,
            reason=reason,
            delivery="EXECUTED" if ok else "NOT_SENT",
        )
