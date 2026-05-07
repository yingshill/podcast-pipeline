"""SQLite-backed job state — lets the pipeline resume after any failure."""
from __future__ import annotations
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from models import PipelineJob, PipelineStatus

DB_PATH = Path(__file__).parent / "pipeline_state.db"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _conn() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db() -> None:
    with _conn() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                url TEXT NOT NULL,
                status TEXT NOT NULL,
                assemblyai_id TEXT,
                doc_id TEXT,
                doc_url TEXT,
                notion_page_id TEXT,
                notion_page_url TEXT,
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)


def create_job(url: str) -> PipelineJob:
    init_db()
    job = PipelineJob(
        id=str(uuid.uuid4())[:8],
        url=url,
        status=PipelineStatus.PENDING,
        created_at=_now(),
        updated_at=_now(),
    )
    with _conn() as con:
        con.execute(
            "INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (job.id, job.url, job.status, None, None, None, None, None, None,
             job.created_at, job.updated_at),
        )
    return job


def save_job(job: PipelineJob) -> None:
    init_db()
    job.updated_at = _now()
    with _conn() as con:
        con.execute(
            """UPDATE jobs SET status=?, assemblyai_id=?, doc_id=?, doc_url=?,
               notion_page_id=?, notion_page_url=?, error=?, updated_at=?
               WHERE id=?""",
            (job.status, job.assemblyai_id, job.doc_id, job.doc_url,
             job.notion_page_id, job.notion_page_url, job.error,
             job.updated_at, job.id),
        )


def get_job(job_id: str) -> Optional[PipelineJob]:
    init_db()
    with _conn() as con:
        row = con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["status"] = PipelineStatus(d["status"])
    return PipelineJob(**d)


def list_jobs() -> list[PipelineJob]:
    init_db()
    with _conn() as con:
        rows = con.execute("SELECT * FROM jobs ORDER BY created_at DESC").fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["status"] = PipelineStatus(d["status"])
        result.append(PipelineJob(**d))
    return result
