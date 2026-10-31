from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

from forge.repair import FixPlan, RepairNotPermitted
from forge.filesystem_repair import FilesystemRepairExecutor


class AtlasFilesystemTaskRunner:
    """Governed ATLAS filesystem mutation adapter."""

    TOOL_NAME = "local_mutation"

    def __init__(
        self,
        executor: FilesystemRepairExecutor,
    ) -> None:
        self.executor = executor
        self._repair_results = {}

    @staticmethod
    def _parse(task: Any) -> tuple[str, str]:

        tools = list(
            getattr(task, "tools", None) or []
        )

        if AtlasFilesystemTaskRunner.TOOL_NAME not in tools:
            raise RepairNotPermitted(
                "task not authorized for local_mutation"
            )

        resources = list(
            getattr(task, "resources", None) or []
        )

        target = None
        content_b64 = None

        for item in resources:

            if not isinstance(item, str):
                continue

            if item.startswith("target:"):
                target = item.split(":", 1)[1].strip()

            elif item.startswith("content_b64:"):
                content_b64 = item.split(":", 1)[1].strip()

        if not target:
            raise RepairNotPermitted(
                "local mutation requires target:<resource>"
            )

        if not content_b64:
            raise RepairNotPermitted(
                "local mutation requires content_b64:<value>"
            )

        try:
            content = base64.b64decode(
                content_b64,
                validate=True,
            ).decode("utf-8")

        except Exception as exc:
            raise RepairNotPermitted(
                "invalid UTF-8 base64 content"
            ) from exc

        return target, content

    def __call__(self, task: Any) -> dict:

        target, content = self._parse(task)

        plan = FixPlan.new(
            diagnostic_id=(
                "atlas_local_mutation_"
                + str(getattr(task, "task_id", "unknown"))
            ),
            executor_name="ATLAS_PHASE2",
            changes={
                target: content,
            },
            notes="Governed ATLAS filesystem mutation",
        )

        dry = self.executor.dry_run(plan)

        applied = self.executor.apply(plan)

        if not applied.applied:
            return {
                "executor": "ATLAS_FILESYSTEM_REPAIR",
                "task_id": getattr(task, "task_id", None),
                "target": target,
                "ok": False,
                "dry_run": dry.dry_run,
                "applied": False,
                "verified": False,
                "error": applied.error,
            }

        absolute_target = (
            self.executor.project_root / target
        ).resolve(strict=False)

        verified = (
            absolute_target.exists()
            and absolute_target.read_text(
                encoding="utf-8"
            ) == content
        )

        if not verified:
            self.executor.rollback(applied)

            return {
                "executor": "ATLAS_FILESYSTEM_REPAIR",
                "task_id": getattr(task, "task_id", None),
                "target": target,
                "ok": False,
                "dry_run": dry.dry_run,
                "applied": True,
                "verified": False,
                "error": "post-write verification failed; rolled back",
            }

        task_id = str(
            getattr(task, "task_id", "unknown")
        )

        self._repair_results[task_id] = applied

        return {
            "executor": "ATLAS_FILESYSTEM_REPAIR",
            "task_id": task_id,
            "target": target,
            "ok": True,
            "dry_run": dry.dry_run,
            "applied": True,
            "verified": True,
            "physical_backup": bool(applied.backup),
            "plan_id": plan.plan_id,
            "error": None,
        }

    def rollback_task(
        self,
        task_id: str,
    ) -> bool:

        result = self._repair_results.get(task_id)

        if result is None:
            return False

        self.executor.rollback(result)

        return True
