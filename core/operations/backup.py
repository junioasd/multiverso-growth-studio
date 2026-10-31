"""
core.operations.backup — backup operacional do SQLite.
Consistente (sqlite3 backup API, online), nome com timestamp UTC, NUNCA
sobrescreve (cria arquivo novo; colisão => sufixo), SHA-256 gravado ao lado,
integrity_check no arquivo gerado, retenção configurável (só remove backups
deste esquema de nomes, mais antigos), relatório JSONL de sucesso/falha.
Destino externo NÃO é inventado: sem BACKUP_EXTERNAL_TARGET => LOCAL_BACKUP_ONLY.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import sqlite3

PREFIX = "mgs_backup_"


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def run_backup(*, db_path: str, backup_dir: str, retention: int = 14, external_target: str | None = None) -> dict:
    os.makedirs(backup_dir, exist_ok=True)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    target = os.path.join(backup_dir, f"{PREFIX}{stamp}.db")
    n = 1
    while os.path.exists(target):
        target = os.path.join(backup_dir, f"{PREFIX}{stamp}_{n}.db")
        n += 1
    report = {"started_at": stamp, "db_path": db_path, "backup_file": target,
              "mode": "EXTERNAL_COPY_PENDING" if external_target else "LOCAL_BACKUP_ONLY"}
    try:
        if not os.path.isfile(db_path):
            raise FileNotFoundError(f"banco inexistente: {db_path}")
        src = sqlite3.connect(db_path)
        dst = sqlite3.connect(target)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        chk = sqlite3.connect(target)
        try:
            integrity = chk.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            chk.close()
        digest = _sha256(target)
        with open(target + ".sha256", "x", encoding="ascii") as f:
            f.write(f"{digest}  {os.path.basename(target)}\n")
        report.update(status="SUCCESS" if integrity == "ok" else "FAILED_INTEGRITY", integrity_check=integrity,
                      sha256=digest, size_bytes=os.path.getsize(target))
        if external_target:
            report["external_note"] = "destino externo configurado mas cópia externa não implementada neste candidato — pendência operacional"
    except Exception as exc:
        report.update(status="FAILED", error=f"{type(exc).__name__}: {exc}")
    backups = sorted(f for f in os.listdir(backup_dir) if f.startswith(PREFIX) and f.endswith(".db"))
    removed = []
    if report.get("status") == "SUCCESS" and retention > 0 and len(backups) > retention:
        for old in backups[: len(backups) - retention]:
            for p in (old, old + ".sha256"):
                fp = os.path.join(backup_dir, p)
                if os.path.exists(fp):
                    os.remove(fp)
            removed.append(old)
    report["retention"], report["removed_by_retention"] = retention, removed
    with open(os.path.join(backup_dir, "backup_report.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(report, ensure_ascii=False) + "\n")
    return report


def verify_backup(path: str) -> bool:
    with open(path + ".sha256", encoding="ascii") as f:
        expected = f.read().split()[0]
    return _sha256(path) == expected
