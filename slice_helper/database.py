from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

import aiosqlite


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class Database:
    JOB_FIELDS = {
        "status",
        "progress",
        "current_window",
        "next_window_start",
        "total_windows",
        "pause_requested",
        "stop_requested",
        "error_message",
        "warnings_json",
        "started_at",
        "completed_at",
    }
    WINDOW_FIELDS = {
        "requested_start",
        "nominal_end",
        "chunk_path",
        "chunk_url",
        "status",
        "handoff_start",
        "error_message",
    }
    ATTEMPT_FIELDS = {"status", "raw_response_path", "error_message", "finished_at"}

    def __init__(self, path: Path):
        self.path = path

    @asynccontextmanager
    async def connect(self):
        db = await aiosqlite.connect(self.path, timeout=15.0)
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys=ON")
        await db.execute("PRAGMA busy_timeout=15000")
        try:
            yield db
        finally:
            await db.close()

    async def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        async with self.connect() as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_version (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    source_path TEXT NOT NULL,
                    source_size INTEGER NOT NULL,
                    source_mtime_ns INTEGER NOT NULL,
                    source_duration REAL NOT NULL,
                    template_id TEXT NOT NULL,
                    language TEXT NOT NULL,
                    channel_name TEXT NOT NULL DEFAULT '',
                    program_start_time TEXT,
                    cut_mode TEXT NOT NULL,
                    status TEXT NOT NULL,
                    progress REAL NOT NULL DEFAULT 0,
                    current_window INTEGER NOT NULL DEFAULT 0,
                    next_window_start REAL NOT NULL DEFAULT 0,
                    total_windows INTEGER NOT NULL,
                    pause_requested INTEGER NOT NULL DEFAULT 0,
                    stop_requested INTEGER NOT NULL DEFAULT 0,
                    error_message TEXT NOT NULL DEFAULT '',
                    warnings_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_jobs_status_created
                    ON jobs(status, created_at);

                CREATE TABLE IF NOT EXISTS windows (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                    window_index INTEGER NOT NULL,
                    requested_start REAL NOT NULL,
                    nominal_end REAL NOT NULL,
                    chunk_path TEXT NOT NULL DEFAULT '',
                    chunk_url TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    handoff_start REAL,
                    error_message TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(job_id, window_index)
                );
                CREATE INDEX IF NOT EXISTS idx_windows_job ON windows(job_id, window_index);

                CREATE TABLE IF NOT EXISTS attempts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    window_id INTEGER NOT NULL REFERENCES windows(id) ON DELETE CASCADE,
                    attempt_no INTEGER NOT NULL,
                    task_id TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL,
                    raw_response_path TEXT NOT NULL DEFAULT '',
                    error_message TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    finished_at TEXT,
                    UNIQUE(window_id, attempt_no)
                );

                CREATE TABLE IF NOT EXISTS segments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                    window_id INTEGER NOT NULL REFERENCES windows(id) ON DELETE CASCADE,
                    source_index INTEGER NOT NULL,
                    accepted INTEGER NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    local_start REAL NOT NULL,
                    local_end REAL NOT NULL,
                    global_start REAL NOT NULL,
                    global_end REAL NOT NULL,
                    absolute_start TEXT,
                    absolute_end TEXT,
                    title TEXT NOT NULL DEFAULT '',
                    topic TEXT NOT NULL DEFAULT '',
                    keywords_json TEXT NOT NULL DEFAULT '[]',
                    summary TEXT NOT NULL DEFAULT '',
                    segment_url TEXT NOT NULL DEFAULT '',
                    cover_img_url TEXT NOT NULL DEFAULT '',
                    raw_json TEXT NOT NULL,
                    UNIQUE(window_id, source_index)
                );
                CREATE INDEX IF NOT EXISTS idx_segments_job_time
                    ON segments(job_id, accepted, global_start);
                """
            )
            await db.execute(
                "INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES(1, ?)",
                (utc_now(),),
            )
            await db.commit()

    @staticmethod
    def _row(row: aiosqlite.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    async def create_job(self, record: dict[str, Any]) -> None:
        now = utc_now()
        values = {
            **record,
            "status": record.get("status", "queued"),
            "progress": 0.0,
            "current_window": 0,
            "next_window_start": 0.0,
            "pause_requested": 0,
            "stop_requested": 0,
            "error_message": "",
            "warnings_json": "[]",
            "created_at": now,
            "updated_at": now,
        }
        columns = ", ".join(values)
        placeholders = ", ".join("?" for _ in values)
        async with self.connect() as db:
            await db.execute(
                f"INSERT INTO jobs ({columns}) VALUES ({placeholders})",
                tuple(values.values()),
            )
            await db.commit()

    async def get_job(self, job_id: str) -> dict[str, Any] | None:
        async with self.connect() as db:
            row = await (await db.execute("SELECT * FROM jobs WHERE id=?", (job_id,))).fetchone()
        return self._row(row)

    async def list_jobs(self, status: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        query = """
            SELECT j.*,
                   COUNT(DISTINCT w.id) AS window_count,
                   COUNT(DISTINCT CASE WHEN s.accepted=1 THEN s.id END) AS accepted_segment_count
            FROM jobs j
            LEFT JOIN windows w ON w.job_id=j.id
            LEFT JOIN segments s ON s.job_id=j.id
        """
        params: list[Any] = []
        if status:
            query += " WHERE j.status=?"
            params.append(status)
        query += " GROUP BY j.id ORDER BY j.created_at DESC LIMIT ?"
        params.append(limit)
        async with self.connect() as db:
            rows = await (await db.execute(query, params)).fetchall()
        return [dict(row) for row in rows]

    async def update_job(self, job_id: str, **fields: Any) -> None:
        unknown = set(fields) - self.JOB_FIELDS
        if unknown:
            raise ValueError(f"Unknown job fields: {sorted(unknown)}")
        if not fields:
            return
        fields["updated_at"] = utc_now()
        assignments = ", ".join(f"{key}=?" for key in fields)
        async with self.connect() as db:
            await db.execute(
                f"UPDATE jobs SET {assignments} WHERE id=?",
                (*fields.values(), job_id),
            )
            await db.commit()

    async def recover_jobs(self) -> None:
        now = utc_now()
        async with self.connect() as db:
            await db.execute(
                """
                UPDATE jobs SET status='queued', updated_at=?
                WHERE status IN ('probing', 'running')
                """,
                (now,),
            )
            await db.execute(
                """
                UPDATE jobs SET status='paused', pause_requested=0, updated_at=?
                WHERE status='pause_requested'
                """,
                (now,),
            )
            await db.execute(
                """
                UPDATE jobs SET status='stopped', stop_requested=0, updated_at=?
                WHERE status='stop_requested'
                """,
                (now,),
            )
            await db.commit()

    async def get_runnable_jobs(self, limit: int) -> list[dict[str, Any]]:
        async with self.connect() as db:
            rows = await (
                await db.execute(
                    "SELECT * FROM jobs WHERE status='queued' ORDER BY created_at LIMIT ?",
                    (limit,),
                )
            ).fetchall()
        return [dict(row) for row in rows]

    async def append_warning(self, job_id: str, warning: str) -> None:
        job = await self.get_job(job_id)
        if not job:
            return
        warnings = json.loads(job.get("warnings_json") or "[]")
        if warning not in warnings:
            warnings.append(warning)
            await self.update_job(job_id, warnings_json=json.dumps(warnings, ensure_ascii=False))

    async def upsert_window(
        self, job_id: str, window_index: int, requested_start: float, nominal_end: float
    ) -> dict[str, Any]:
        now = utc_now()
        async with self.connect() as db:
            await db.execute(
                """
                INSERT INTO windows(
                    job_id, window_index, requested_start, nominal_end, status, created_at, updated_at
                ) VALUES(?, ?, ?, ?, 'pending', ?, ?)
                ON CONFLICT(job_id, window_index) DO NOTHING
                """,
                (job_id, window_index, requested_start, nominal_end, now, now),
            )
            await db.commit()
            row = await (
                await db.execute(
                    "SELECT * FROM windows WHERE job_id=? AND window_index=?",
                    (job_id, window_index),
                )
            ).fetchone()
        if row is None:
            raise RuntimeError("Failed to create window")
        return dict(row)

    async def get_window(self, job_id: str, window_index: int) -> dict[str, Any] | None:
        async with self.connect() as db:
            row = await (
                await db.execute(
                    "SELECT * FROM windows WHERE job_id=? AND window_index=?",
                    (job_id, window_index),
                )
            ).fetchone()
        return self._row(row)

    async def get_windows(self, job_id: str) -> list[dict[str, Any]]:
        async with self.connect() as db:
            rows = await (
                await db.execute(
                    "SELECT * FROM windows WHERE job_id=? ORDER BY window_index",
                    (job_id,),
                )
            ).fetchall()
        return [dict(row) for row in rows]

    async def update_window(self, window_id: int, **fields: Any) -> None:
        unknown = set(fields) - self.WINDOW_FIELDS
        if unknown:
            raise ValueError(f"Unknown window fields: {sorted(unknown)}")
        fields["updated_at"] = utc_now()
        assignments = ", ".join(f"{key}=?" for key in fields)
        async with self.connect() as db:
            await db.execute(
                f"UPDATE windows SET {assignments} WHERE id=?",
                (*fields.values(), window_id),
            )
            await db.commit()

    async def get_attempts(self, window_id: int) -> list[dict[str, Any]]:
        async with self.connect() as db:
            rows = await (
                await db.execute(
                    "SELECT * FROM attempts WHERE window_id=? ORDER BY attempt_no",
                    (window_id,),
                )
            ).fetchall()
        return [dict(row) for row in rows]

    async def create_attempt(
        self, window_id: int, attempt_no: int, task_id: str
    ) -> dict[str, Any]:
        async with self.connect() as db:
            await db.execute(
                """
                INSERT OR IGNORE INTO attempts(
                    window_id, attempt_no, task_id, status, created_at
                ) VALUES(?, ?, ?, 'pending', ?)
                """,
                (window_id, attempt_no, task_id, utc_now()),
            )
            await db.commit()
            row = await (
                await db.execute(
                    "SELECT * FROM attempts WHERE window_id=? AND attempt_no=?",
                    (window_id, attempt_no),
                )
            ).fetchone()
        if row is None:
            raise RuntimeError("Failed to create attempt")
        return dict(row)

    async def update_attempt(self, attempt_id: int, **fields: Any) -> None:
        unknown = set(fields) - self.ATTEMPT_FIELDS
        if unknown:
            raise ValueError(f"Unknown attempt fields: {sorted(unknown)}")
        assignments = ", ".join(f"{key}=?" for key in fields)
        async with self.connect() as db:
            await db.execute(
                f"UPDATE attempts SET {assignments} WHERE id=?",
                (*fields.values(), attempt_id),
            )
            await db.commit()

    async def replace_window_segments(
        self, job_id: str, window_id: int, segments: Iterable[dict[str, Any]]
    ) -> None:
        columns = (
            "job_id",
            "window_id",
            "source_index",
            "accepted",
            "reason",
            "local_start",
            "local_end",
            "global_start",
            "global_end",
            "absolute_start",
            "absolute_end",
            "title",
            "topic",
            "keywords_json",
            "summary",
            "segment_url",
            "cover_img_url",
            "raw_json",
        )
        async with self.connect() as db:
            await db.execute("DELETE FROM segments WHERE window_id=?", (window_id,))
            for segment in segments:
                row = {**segment, "job_id": job_id, "window_id": window_id}
                await db.execute(
                    f"INSERT INTO segments ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
                    tuple(row[column] for column in columns),
                )
            await db.commit()

    async def get_segments(
        self, job_id: str, accepted_only: bool = False
    ) -> list[dict[str, Any]]:
        query = """
            SELECT s.*, w.window_index
            FROM segments s JOIN windows w ON w.id=s.window_id
            WHERE s.job_id=?
        """
        params: list[Any] = [job_id]
        if accepted_only:
            query += " AND s.accepted=1"
        query += " ORDER BY s.global_start, s.global_end, s.id"
        async with self.connect() as db:
            rows = await (await db.execute(query, params)).fetchall()
        return [dict(row) for row in rows]

    async def get_attempts_for_job(self, job_id: str) -> list[dict[str, Any]]:
        async with self.connect() as db:
            rows = await (
                await db.execute(
                    """
                    SELECT a.*, w.window_index
                    FROM attempts a JOIN windows w ON w.id=a.window_id
                    WHERE w.job_id=? ORDER BY w.window_index, a.attempt_no
                    """,
                    (job_id,),
                )
            ).fetchall()
        return [dict(row) for row in rows]

    async def ping(self) -> bool:
        try:
            async with self.connect() as db:
                row = await (await db.execute("SELECT 1")).fetchone()
            return bool(row and row[0] == 1)
        except Exception:
            return False
