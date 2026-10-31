from __future__ import annotations

import json
import os
import sqlite3
import uuid

from mcp.server.mcpserver import MCPServer

from core.jobs.queue import JobQueue


DB_PATH = os.environ.get(
    "MGS_DB_PATH",
    r"C:\Users\COMFORMAK\GrowthStudio-Data\mgs.db",
)

ATLAS_TASK_JOB = "ATLAS_TASK"

MCP_HOST = "127.0.0.1"
MCP_PORT = 8792
MCP_PATH = "/mcp"


server = MCPServer(
    "multiverso-growth-atlas"
)


def _queue() -> JobQueue:
    return JobQueue(
        db_path=DB_PATH,
        max_concurrent_running=10,
        default_timeout_seconds=300.0,
    )


@server.tool()
def atlas_health() -> dict:
    """Check the local ATLAS bridge and persistent Growth job database."""

    con = sqlite3.connect(DB_PATH)

    try:
        tables = {
            row[0]
            for row in con.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table'"
            )
        }
    finally:
        con.close()

    return {
        "ok": "jobs" in tables,
        "component": "MULTIVERSO_GROWTH_ATLAS_MCP",
        "jobs_table": "jobs" in tables,
    }


@server.tool()
def atlas_submit_task(
    task_type: str,
    payload_json: str = "{}",
    preferred_agent: str | None = None,
    idempotency_key: str | None = None,
) -> dict:
    """Submit a governed task to ATLAS through the Growth persistent queue."""

    try:
        payload = json.loads(payload_json)
    except json.JSONDecodeError as exc:
        raise ValueError(
            "payload_json must contain valid JSON"
        ) from exc

    if not isinstance(payload, dict):
        raise ValueError(
            "payload_json must decode to an object"
        )

    task_type = str(task_type).strip()

    if not task_type:
        raise ValueError(
            "task_type is required"
        )

    body = dict(payload)
    body["task_type"] = task_type

    if preferred_agent:
        body["preferred_agent"] = str(
            preferred_agent
        )

    key = (
        str(idempotency_key).strip()
        if idempotency_key
        else "chat-atlas-" + uuid.uuid4().hex
    )

    job = _queue().enqueue(
        job_type=ATLAS_TASK_JOB,
        idempotency_key=key,
        payload=body,
    )

    return {
        "job_id": job.job_id,
        "state": getattr(
            job.state,
            "value",
            str(job.state),
        ),
        "idempotency_key": job.idempotency_key,
    }


@server.tool()
def atlas_get_task(
    job_id: str,
) -> dict:
    """Read the status, result or error of an ATLAS task."""

    job = _queue().get(
        str(job_id)
    )

    if job is None:
        return {
            "found": False,
            "job_id": str(job_id),
        }

    return {
        "found": True,
        "job_id": job.job_id,
        "job_type": job.job_type,
        "state": getattr(
            job.state,
            "value",
            str(job.state),
        ),
        "attempts": job.attempts,
        "max_attempts": job.max_attempts,
        "result": job.result,
        "error": job.error,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
    }


if __name__ == "__main__":
    server.run(
        transport="streamable-http",
        host=MCP_HOST,
        port=MCP_PORT,
        streamable_http_path=MCP_PATH,
        stateless_http=True,
    )
