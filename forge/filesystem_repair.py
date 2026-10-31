"""Real filesystem repair layer for FORGE.

Safety properties:
- exact resource allowlist;
- project-root confinement;
- physical backup;
- atomic UTF-8 writes;
- rollback of existing and newly-created files;
- audit log without file contents;
- no shell execution.
"""

from __future__ import annotations

from pathlib import Path
import hashlib
import json
import os
import shutil
import tempfile
import time

from forge.repair import (
    FixPlan,
    RepairResult,
    RepairNotPermitted,
)


class FilesystemRepairExecutor:
    def __init__(
        self,
        *,
        project_root: str | Path,
        allowed_resources: set[str],
        backup_root: str | Path,
        audit_path: str | Path,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.allowed_resources = set(allowed_resources)

        self.backup_root = Path(backup_root)
        self.backup_root.mkdir(parents=True, exist_ok=True)

        self.audit_path = Path(audit_path)
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _sha256_text(value: str) -> str:
        return hashlib.sha256(
            value.encode("utf-8")
        ).hexdigest()

    def _resolve(self, resource: str) -> Path:
        if resource not in self.allowed_resources:
            raise RepairNotPermitted(
                f"resource not allowlisted: {resource}"
            )

        target = (
            self.project_root / resource
        ).resolve(strict=False)

        if not (
            target == self.project_root
            or target.is_relative_to(self.project_root)
        ):
            raise RepairNotPermitted(
                f"resource outside project root: {resource}"
            )

        return target

    def _audit(
        self,
        *,
        action: str,
        plan_id: str,
        ok: bool,
        resources: list[str],
        error: str | None = None,
    ) -> None:
        record = {
            "timestamp": time.time(),
            "component": "FORGE_FILESYSTEM_REPAIR",
            "action": action,
            "plan_id": plan_id,
            "ok": ok,
            "resources": resources,
            "error_type": (
                type(error).__name__
                if error is not None
                else None
            ),
        }

        with self.audit_path.open(
            "a",
            encoding="utf-8",
        ) as fh:
            fh.write(
                json.dumps(
                    record,
                    ensure_ascii=False,
                    sort_keys=True,
                )
                + "\n"
            )

    def dry_run(
        self,
        plan: FixPlan,
    ) -> RepairResult:

        diff = {}

        for resource, new_value in plan.changes.items():
            if not isinstance(new_value, str):
                raise RepairNotPermitted(
                    "filesystem repair accepts text values only"
                )

            target = self._resolve(resource)

            old_value = (
                target.read_text(
                    encoding="utf-8"
                )
                if target.exists()
                else ""
            )

            diff[resource] = {
                "exists_before": target.exists(),
                "old_sha256": self._sha256_text(old_value),
                "new_sha256": self._sha256_text(new_value),
                "changed": old_value != new_value,
            }

        result = RepairResult(
            plan_id=plan.plan_id,
            applied=False,
            dry_run=True,
            backup={},
            diff=diff,
            error=None,
        )

        self._audit(
            action="dry_run",
            plan_id=plan.plan_id,
            ok=True,
            resources=list(plan.changes),
        )

        return result

    def apply(
        self,
        plan: FixPlan,
    ) -> RepairResult:

        backup = {}
        diff = {}

        plan_backup = (
            self.backup_root / plan.plan_id
        )

        plan_backup.mkdir(
            parents=True,
            exist_ok=True,
        )

        applied_resources = []

        try:
            for resource, new_value in plan.changes.items():

                if not isinstance(new_value, str):
                    raise RepairNotPermitted(
                        "filesystem repair accepts text values only"
                    )

                target = self._resolve(resource)

                target.parent.mkdir(
                    parents=True,
                    exist_ok=True,
                )

                existed = target.exists()

                old_value = (
                    target.read_text(
                        encoding="utf-8"
                    )
                    if existed
                    else ""
                )

                backup_file = (
                    plan_backup
                    / (
                        resource
                        .replace("\\", "__")
                        .replace("/", "__")
                        + ".bak"
                    )
                )

                if existed:
                    shutil.copy2(
                        target,
                        backup_file,
                    )

                backup[resource] = {
                    "target": str(target),
                    "existed": existed,
                    "backup_path": (
                        str(backup_file)
                        if existed
                        else None
                    ),
                }

                diff[resource] = {
                    "old_sha256": self._sha256_text(old_value),
                    "new_sha256": self._sha256_text(new_value),
                    "changed": old_value != new_value,
                }

                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    delete=False,
                    dir=str(target.parent),
                    prefix=".mgs_tmp_",
                ) as fh:
                    fh.write(new_value)
                    temp_name = fh.name

                os.replace(
                    temp_name,
                    target,
                )

                applied_resources.append(resource)

            result = RepairResult(
                plan_id=plan.plan_id,
                applied=True,
                dry_run=False,
                backup=backup,
                diff=diff,
                error=None,
                applied_at=time.time(),
            )

            self._audit(
                action="apply",
                plan_id=plan.plan_id,
                ok=True,
                resources=list(plan.changes),
            )

            return result

        except Exception as exc:

            partial = RepairResult(
                plan_id=plan.plan_id,
                applied=False,
                dry_run=False,
                backup=backup,
                diff=diff,
                error=str(exc),
            )

            try:
                self.rollback(partial)
            except Exception:
                pass

            self._audit(
                action="apply",
                plan_id=plan.plan_id,
                ok=False,
                resources=applied_resources,
                error=exc,
            )

            return partial

    def rollback(
        self,
        result: RepairResult,
    ) -> None:

        for resource, meta in result.backup.items():

            target = Path(meta["target"])

            if meta["existed"]:
                backup_path = Path(
                    meta["backup_path"]
                )

                target.parent.mkdir(
                    parents=True,
                    exist_ok=True,
                )

                shutil.copy2(
                    backup_path,
                    target,
                )

            else:
                if target.exists():
                    target.unlink()

        self._audit(
            action="rollback",
            plan_id=result.plan_id,
            ok=True,
            resources=list(result.backup),
        )
