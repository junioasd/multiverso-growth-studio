"""Controlled local execution for ATLAS.

Phase 2 rule:
- no arbitrary shell supplied by an agent;
- only named/read-only operations are accepted here;
- subprocess uses shell=False;
- target paths are constrained to approved local roots;
- stdout/stderr/exit code/timeout are returned;
- audit records never persist command output.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import os
import shutil
import subprocess
import time
from typing import Final


class LocalExecutionPolicyViolation(RuntimeError):
    """Requested operation is outside the local execution policy."""


@dataclass(frozen=True)
class LocalCommandResult:
    action: str
    ok: bool
    returncode: int | None
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool


class LocalCommandExecutor:
    """Fail-closed, read-only local executor for the first ATLAS milestone."""

    _ACTIONS: Final[dict[str, tuple[str, bool]]] = {
        "get_location": (
            "$PWD.Path",
            False,
        ),
        "test_path": (
            "Test-Path -LiteralPath $env:MGS_TARGET_PATH",
            True,
        ),
        "list_directory": (
            "Get-ChildItem -LiteralPath $env:MGS_TARGET_PATH "
            "| Select-Object -ExpandProperty Name",
            True,
        ),
        "get_file_metadata": (
            "Get-Item -LiteralPath $env:MGS_TARGET_PATH "
            "| Select-Object FullName,Length,LastWriteTime "
            "| ConvertTo-Json -Compress",
            True,
        ),
    }

    def __init__(
        self,
        *,
        project_root: str | Path | None = None,
        log_path: str | Path | None = None,
    ) -> None:
        self.project_root = (
            Path(project_root).resolve()
            if project_root is not None
            else Path(__file__).resolve().parents[2]
        )

        self.allowed_roots = tuple(
            p.resolve()
            for p in (
                self.project_root,
                Path(r"C:\Users\COMFORMAK\GrowthStudio-Data"),
                Path(r"C:\Users\COMFORMAK\GrowthStudio-Logs"),
                Path(r"C:\Users\COMFORMAK\GrowthStudio-Backups"),
            )
            if p.exists()
        )

        self.log_path = (
            Path(log_path)
            if log_path is not None
            else Path(
                r"C:\Users\COMFORMAK\GrowthStudio-Logs"
                r"\atlas_local_executor.jsonl"
            )
        )

        self.log_path.parent.mkdir(parents=True, exist_ok=True)

        self.powershell = (
            shutil.which("powershell.exe")
            or shutil.which("pwsh.exe")
        )

        if not self.powershell:
            raise RuntimeError("PowerShell executable not found")

    def _resolve_target(self, target: str | Path) -> Path:
        value = Path(target)

        if not value.is_absolute():
            value = self.project_root / value

        resolved = value.resolve(strict=False)

        allowed = any(
            resolved == root or resolved.is_relative_to(root)
            for root in self.allowed_roots
        )

        if not allowed:
            raise LocalExecutionPolicyViolation(
                f"target outside approved roots: {resolved}"
            )

        return resolved

    def _audit(
        self,
        *,
        action: str,
        target: str | None,
        result: LocalCommandResult,
    ) -> None:
        record = {
            "timestamp": time.time(),
            "component": "ATLAS_LOCAL_EXECUTOR",
            "action": action,
            "target": target,
            "ok": result.ok,
            "returncode": result.returncode,
            "duration_seconds": result.duration_seconds,
            "timed_out": result.timed_out,
        }

        with self.log_path.open(
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

    def execute_readonly(
        self,
        action: str,
        *,
        target: str | Path | None = None,
        timeout_seconds: float = 10.0,
    ) -> LocalCommandResult:

        definition = self._ACTIONS.get(action)

        if definition is None:
            raise LocalExecutionPolicyViolation(
                f"action not allowlisted: {action}"
            )

        script, needs_target = definition

        resolved_target: Path | None = None

        if needs_target:
            if target is None:
                raise LocalExecutionPolicyViolation(
                    f"action requires target: {action}"
                )

            resolved_target = self._resolve_target(target)

        elif target is not None:
            raise LocalExecutionPolicyViolation(
                f"action does not accept target: {action}"
            )

        env = os.environ.copy()

        if resolved_target is not None:
            env["MGS_TARGET_PATH"] = str(resolved_target)

        started = time.monotonic()

        try:
            completed = subprocess.run(
                [
                    self.powershell,
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    script,
                ],
                cwd=str(self.project_root),
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                shell=False,
            )

            duration = time.monotonic() - started

            result = LocalCommandResult(
                action=action,
                ok=(completed.returncode == 0),
                returncode=completed.returncode,
                stdout=completed.stdout,
                stderr=completed.stderr,
                duration_seconds=duration,
                timed_out=False,
            )

        except subprocess.TimeoutExpired as exc:
            duration = time.monotonic() - started

            result = LocalCommandResult(
                action=action,
                ok=False,
                returncode=None,
                stdout=(
                    exc.stdout
                    if isinstance(exc.stdout, str)
                    else ""
                ),
                stderr=(
                    exc.stderr
                    if isinstance(exc.stderr, str)
                    else ""
                ),
                duration_seconds=duration,
                timed_out=True,
            )

        self._audit(
            action=action,
            target=(
                str(resolved_target)
                if resolved_target is not None
                else None
            ),
            result=result,
        )

        return result
