"""
DB-backed import job store.

Replaces the in-process IMPORT_JOBS dict so job state survives API
restarts and is visible across multiple API replicas.

All writes use the legacy get_db_connection() (direct connection) so
they work safely from background threads outside the request lifecycle.
The read endpoint (job status poll) uses the pool via get_conn().
"""

import uuid
from datetime import datetime, timezone

from app.api.db import get_db_connection


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_job(title: str) -> str:
    """Insert a new job row and return the job_id."""
    job_id = str(uuid.uuid4())
    with get_db_connection() as conn:
        conn.execute(
            """
            INSERT INTO import_jobs (job_id, title, status, done, ok, logs, created_at, updated_at)
            VALUES (%s, %s, 'queued', FALSE, FALSE, %s, NOW(), NOW())
            """,
            (job_id, title, f"[{_now_iso()}] Queued {title}\n"),
        )
        conn.commit()
    return job_id


def append_log(job_id: str, message: str) -> None:
    """Append a timestamped line to the job log."""
    with get_db_connection() as conn:
        conn.execute(
            """
            UPDATE import_jobs
            SET logs = logs || %s,
                updated_at = NOW()
            WHERE job_id = %s
            """,
            (f"[{_now_iso()}] {message}\n", job_id),
        )
        conn.commit()


def append_raw(job_id: str, text: str) -> None:
    """Append raw text (e.g. subprocess output) without a timestamp prefix."""
    with get_db_connection() as conn:
        conn.execute(
            """
            UPDATE import_jobs
            SET logs = logs || %s,
                updated_at = NOW()
            WHERE job_id = %s
            """,
            (text, job_id),
        )
        conn.commit()


def set_running(job_id: str) -> None:
    with get_db_connection() as conn:
        conn.execute(
            "UPDATE import_jobs SET status = 'running', done = FALSE, updated_at = NOW() WHERE job_id = %s",
            (job_id,),
        )
        conn.commit()


def finish_job(job_id: str, *, ok: bool, exit_code: int | None = None, message: str = "") -> None:
    status = "finished" if ok else "failed"
    with get_db_connection() as conn:
        if message:
            conn.execute(
                """
                UPDATE import_jobs
                SET status = %s, done = TRUE, ok = %s, exit_code = %s,
                    logs = logs || %s,
                    updated_at = NOW()
                WHERE job_id = %s
                """,
                (status, ok, exit_code, f"[{_now_iso()}] {message}\n", job_id),
            )
        else:
            conn.execute(
                """
                UPDATE import_jobs
                SET status = %s, done = TRUE, ok = %s, exit_code = %s, updated_at = NOW()
                WHERE job_id = %s
                """,
                (status, ok, exit_code, job_id),
            )
        conn.commit()


def get_job(job_id: str) -> dict | None:
    """Return job dict or None if not found. Uses a direct connection (called from any context)."""
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT job_id, title, status, done, ok, exit_code, logs FROM import_jobs WHERE job_id = %s",
            (job_id,),
        ).fetchone()
    if row is None:
        return None
    status = str(row[2])
    done = bool(row[3]) or status in ("finished", "failed")
    return {
        "job_id": str(row[0]),
        "title": str(row[1]),
        "status": status,
        "done": done,
        "ok": bool(row[4]),
        "exit_code": int(row[5]) if row[5] is not None else None,
        "logs": str(row[6]) if row[6] is not None else "",
    }
