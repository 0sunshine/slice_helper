from __future__ import annotations

import json
import re
import unicodedata
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

import aiosqlite


SOURCE_DATE_RE = re.compile(r"(?:^|[_-])(20\d{2})(\d{2})(\d{2})(?:[-_]|$)")


def normalize_channel_name(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def infer_broadcast_date(source: str, program_start_time: str | None) -> str | None:
    match = SOURCE_DATE_RE.search(source)
    if match:
        return f"{match.group(1)}-{match.group(2)}-{match.group(3)}"
    if program_start_time and re.match(r"^20\d{2}-\d{2}-\d{2}", program_start_time):
        return program_start_time[:10]
    return None


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
        "islice_base_url",
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
    ATTEMPT_FIELDS = {
        "status",
        "service_status",
        "progress",
        "raw_response_path",
        "error_message",
        "finished_at",
    }

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

                CREATE TABLE IF NOT EXISTS channels (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    normalized_name TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    source_path TEXT NOT NULL,
                    source_size INTEGER NOT NULL,
                    source_mtime_ns INTEGER NOT NULL,
                    source_duration REAL NOT NULL,
                    source_url TEXT NOT NULL DEFAULT '',
                    islice_base_url TEXT NOT NULL DEFAULT '',
                    template_id TEXT NOT NULL,
                    language TEXT NOT NULL,
                    channel_id TEXT REFERENCES channels(id),
                    channel_name TEXT NOT NULL DEFAULT '',
                    broadcast_date TEXT,
                    program_start_time TEXT,
                    time_reference_source TEXT NOT NULL DEFAULT '',
                    time_reference_text TEXT NOT NULL DEFAULT '',
                    time_reference_confidence REAL,
                    time_reference_frame_path TEXT NOT NULL DEFAULT '',
                    time_reference_error TEXT NOT NULL DEFAULT '',
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
                    ,superseded_at TEXT
                    ,superseded_by_job_id TEXT
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
                    service_status TEXT NOT NULL DEFAULT '',
                    progress REAL NOT NULL DEFAULT 0,
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
                    content_type TEXT NOT NULL DEFAULT '',
                    news_event_type TEXT NOT NULL DEFAULT '',
                    topic TEXT NOT NULL DEFAULT '',
                    keywords_json TEXT NOT NULL DEFAULT '[]',
                    summary TEXT NOT NULL DEFAULT '',
                    segment_url TEXT NOT NULL DEFAULT '',
                    cover_img_url TEXT NOT NULL DEFAULT '',
                    attempt_id INTEGER REFERENCES attempts(id),
                    task_id TEXT NOT NULL DEFAULT '',
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
            job_columns = {
                row["name"]
                for row in await (await db.execute("PRAGMA table_info(jobs)")).fetchall()
            }
            migrations = {
                "time_reference_source": "TEXT NOT NULL DEFAULT ''",
                "time_reference_text": "TEXT NOT NULL DEFAULT ''",
                "time_reference_confidence": "REAL",
                "time_reference_frame_path": "TEXT NOT NULL DEFAULT ''",
                "time_reference_error": "TEXT NOT NULL DEFAULT ''",
                "islice_base_url": "TEXT NOT NULL DEFAULT ''",
                "source_url": "TEXT NOT NULL DEFAULT ''",
                "channel_id": "TEXT REFERENCES channels(id)",
                "broadcast_date": "TEXT",
                "superseded_at": "TEXT",
                "superseded_by_job_id": "TEXT",
            }
            for column, declaration in migrations.items():
                if column not in job_columns:
                    await db.execute(f"ALTER TABLE jobs ADD COLUMN {column} {declaration}")
            await db.execute(
                "INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES(2, ?)",
                (utc_now(),),
            )
            await db.execute(
                "INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES(3, ?)",
                (utc_now(),),
            )
            version_4 = await (
                await db.execute("SELECT 1 FROM schema_version WHERE version=4")
            ).fetchone()
            if version_4 is None:
                await db.execute(
                    "UPDATE jobs SET status='pending_schedule' WHERE status='queued'"
                )
                await db.execute(
                    "INSERT INTO schema_version(version, applied_at) VALUES(4, ?)",
                    (utc_now(),),
                )
            attempt_columns = {
                row["name"]
                for row in await (await db.execute("PRAGMA table_info(attempts)")).fetchall()
            }
            attempt_migrations = {
                "service_status": "TEXT NOT NULL DEFAULT ''",
                "progress": "REAL NOT NULL DEFAULT 0",
            }
            for column, declaration in attempt_migrations.items():
                if column not in attempt_columns:
                    await db.execute(f"ALTER TABLE attempts ADD COLUMN {column} {declaration}")
            await db.execute(
                "INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES(5, ?)",
                (utc_now(),),
            )
            await db.execute(
                "INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES(6, ?)",
                (utc_now(),),
            )
            segment_columns = {
                row["name"]
                for row in await (await db.execute("PRAGMA table_info(segments)")).fetchall()
            }
            if "content_type" not in segment_columns:
                await db.execute(
                    "ALTER TABLE segments ADD COLUMN content_type TEXT NOT NULL DEFAULT ''"
                )
            if "news_event_type" not in segment_columns:
                await db.execute(
                    "ALTER TABLE segments ADD COLUMN news_event_type TEXT NOT NULL DEFAULT ''"
                )
            await db.execute(
                "INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES(7, ?)",
                (utc_now(),),
            )
            await db.execute(
                "INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES(8, ?)",
                (utc_now(),),
            )
            backfill_rows = await (
                await db.execute(
                    "SELECT id, raw_json FROM segments WHERE news_event_type=''"
                )
            ).fetchall()
            for row in backfill_rows:
                try:
                    raw = json.loads(row["raw_json"] or "{}")
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                news_event_type = str(raw.get("newsEventType") or "")
                if news_event_type:
                    await db.execute(
                        "UPDATE segments SET news_event_type=? WHERE id=?",
                        (news_event_type, row["id"]),
                    )
            await db.execute(
                "INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES(9, ?)",
                (utc_now(),),
            )
            version_10 = await (
                await db.execute("SELECT 1 FROM schema_version WHERE version=10")
            ).fetchone()
            if version_10 is None:
                now = utc_now()
                existing_channels = await (
                    await db.execute("SELECT id, normalized_name FROM channels")
                ).fetchall()
                channel_ids = {
                    row["normalized_name"]: row["id"] for row in existing_channels
                }
                jobs = await (
                    await db.execute(
                        "SELECT id, channel_name, source_path, source_url, "
                        "program_start_time, source_duration, created_at FROM jobs"
                    )
                ).fetchall()
                grouped: dict[tuple[str, str], list[aiosqlite.Row]] = {}
                for row in jobs:
                    name = " ".join(str(row["channel_name"] or "").split())
                    if not name:
                        continue
                    normalized = normalize_channel_name(name)
                    channel_id = channel_ids.get(normalized)
                    if channel_id is None:
                        channel_id = uuid.uuid4().hex
                        await db.execute(
                            "INSERT INTO channels(id, name, normalized_name, created_at, updated_at) "
                            "VALUES(?, ?, ?, ?, ?)",
                            (channel_id, name, normalized, now, now),
                        )
                        channel_ids[normalized] = channel_id
                    source = str(row["source_url"] or row["source_path"] or "")
                    broadcast_date = infer_broadcast_date(
                        source, row["program_start_time"]
                    )
                    await db.execute(
                        "UPDATE jobs SET channel_id=?, channel_name=?, broadcast_date=? WHERE id=?",
                        (channel_id, name, broadcast_date, row["id"]),
                    )
                    if broadcast_date:
                        grouped.setdefault((channel_id, broadcast_date), []).append(row)
                for duplicate_rows in grouped.values():
                    if len(duplicate_rows) < 2:
                        continue
                    winner = max(
                        duplicate_rows,
                        key=lambda row: (
                            float(row["source_duration"] or 0),
                            str(row["created_at"] or ""),
                            str(row["id"]),
                        ),
                    )
                    for row in duplicate_rows:
                        if row["id"] != winner["id"]:
                            await db.execute(
                                "UPDATE jobs SET superseded_at=?, superseded_by_job_id=? WHERE id=?",
                                (now, winner["id"], row["id"]),
                            )
                await db.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_channel_date_current "
                    "ON jobs(channel_id, broadcast_date) "
                    "WHERE channel_id IS NOT NULL AND broadcast_date IS NOT NULL "
                    "AND superseded_at IS NULL"
                )
                await db.execute(
                    "INSERT INTO schema_version(version, applied_at) VALUES(10, ?)",
                    (now,),
                )
            segment_columns = {
                row["name"]
                for row in await (await db.execute("PRAGMA table_info(segments)")).fetchall()
            }
            if "attempt_id" not in segment_columns:
                await db.execute(
                    "ALTER TABLE segments ADD COLUMN attempt_id INTEGER REFERENCES attempts(id)"
                )
            if "task_id" not in segment_columns:
                await db.execute(
                    "ALTER TABLE segments ADD COLUMN task_id TEXT NOT NULL DEFAULT ''"
                )
            await db.execute(
                """
                UPDATE segments
                SET attempt_id=(
                        SELECT a.id FROM attempts a
                        WHERE a.window_id=segments.window_id
                        ORDER BY CASE WHEN a.status='completed' THEN 0 ELSE 1 END,
                                 a.attempt_no DESC LIMIT 1
                    ),
                    task_id=COALESCE((
                        SELECT a.task_id FROM attempts a
                        WHERE a.window_id=segments.window_id
                        ORDER BY CASE WHEN a.status='completed' THEN 0 ELSE 1 END,
                                 a.attempt_no DESC LIMIT 1
                    ), '')
                WHERE attempt_id IS NULL OR task_id=''
                """
            )
            await db.execute(
                "INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES(11, ?)",
                (utc_now(),),
            )
            await db.commit()

    @staticmethod
    def _row(row: aiosqlite.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    async def create_channel(self, name: str) -> dict[str, Any]:
        now = utc_now()
        channel_id = uuid.uuid4().hex
        normalized = normalize_channel_name(name)
        async with self.connect() as db:
            row = await (
                await db.execute(
                    "INSERT INTO channels(id, name, normalized_name, created_at, updated_at) "
                    "VALUES(?, ?, ?, ?, ?) RETURNING *",
                    (channel_id, name, normalized, now, now),
                )
            ).fetchone()
            await db.commit()
        return dict(row)

    async def list_channels(self) -> list[dict[str, Any]]:
        async with self.connect() as db:
            rows = await (
                await db.execute(
                    """
                    SELECT c.*,
                           COUNT(CASE WHEN j.superseded_at IS NULL THEN 1 END) AS job_count
                    FROM channels c
                    LEFT JOIN jobs j ON j.channel_id=c.id
                    GROUP BY c.id
                    ORDER BY c.name, c.id
                    """
                )
            ).fetchall()
        return [dict(row) for row in rows]

    async def get_channel(self, channel_id: str) -> dict[str, Any] | None:
        async with self.connect() as db:
            row = await (
                await db.execute("SELECT * FROM channels WHERE id=?", (channel_id,))
            ).fetchone()
        return self._row(row)

    async def update_channel(self, channel_id: str, name: str) -> dict[str, Any] | None:
        now = utc_now()
        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await (
                await db.execute("SELECT id FROM channels WHERE id=?", (channel_id,))
            ).fetchone()
            if row is None:
                await db.rollback()
                return None
            await db.execute(
                "UPDATE channels SET name=?, normalized_name=?, updated_at=? WHERE id=?",
                (name, normalize_channel_name(name), now, channel_id),
            )
            await db.execute(
                "UPDATE jobs SET channel_name=?, updated_at=? WHERE channel_id=?",
                (name, now, channel_id),
            )
            updated = await (
                await db.execute("SELECT * FROM channels WHERE id=?", (channel_id,))
            ).fetchone()
            await db.commit()
        return dict(updated)

    async def delete_channel(self, channel_id: str) -> bool:
        async with self.connect() as db:
            count = await (
                await db.execute(
                    "SELECT COUNT(*) AS value FROM jobs WHERE channel_id=?", (channel_id,)
                )
            ).fetchone()
            if int(count["value"]):
                raise ValueError("Channel has jobs")
            cursor = await db.execute("DELETE FROM channels WHERE id=?", (channel_id,))
            await db.commit()
        return cursor.rowcount > 0

    async def get_current_job_for_channel_date(
        self, channel_id: str, broadcast_date: str
    ) -> dict[str, Any] | None:
        async with self.connect() as db:
            row = await (
                await db.execute(
                    "SELECT * FROM jobs WHERE channel_id=? AND broadcast_date=? "
                    "AND superseded_at IS NULL",
                    (channel_id, broadcast_date),
                )
            ).fetchone()
        return self._row(row)

    async def create_job(
        self, record: dict[str, Any], supersede_job_id: str | None = None
    ) -> dict[str, Any]:
        now = utc_now()
        values = {
            **record,
            "status": record.get("status", "pending_schedule"),
            "progress": 0.0,
            "current_window": 0,
            "next_window_start": 0.0,
            "pause_requested": 0,
            "stop_requested": 0,
            "error_message": "",
            "warnings_json": record.get("warnings_json", "[]"),
            "created_at": now,
            "updated_at": now,
        }
        columns = ", ".join(values)
        placeholders = ", ".join("?" for _ in values)
        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            if supersede_job_id:
                await db.execute(
                    "UPDATE jobs SET superseded_at=?, superseded_by_job_id=?, updated_at=? "
                    "WHERE id=? AND superseded_at IS NULL",
                    (now, record["id"], now, supersede_job_id),
                )
            row = await (
                await db.execute(
                    f"INSERT INTO jobs ({columns}) VALUES ({placeholders}) RETURNING *",
                    tuple(values.values()),
                )
            ).fetchone()
            if row is None:
                raise RuntimeError("Failed to create job")
            await db.commit()
        return dict(row)

    async def get_job(self, job_id: str) -> dict[str, Any] | None:
        async with self.connect() as db:
            row = await (await db.execute("SELECT * FROM jobs WHERE id=?", (job_id,))).fetchone()
        return self._row(row)

    async def assign_legacy_jobs_with_attempts(self, base_url: str) -> None:
        async with self.connect() as db:
            await db.execute(
                """
                UPDATE jobs SET islice_base_url=?, updated_at=?
                WHERE islice_base_url=''
                  AND EXISTS (
                      SELECT 1 FROM windows w
                      JOIN attempts a ON a.window_id=w.id
                      WHERE w.job_id=jobs.id
                  )
                """,
                (base_url.rstrip("/"), utc_now()),
            )
            await db.commit()

    async def claim_schedulable_jobs(
        self,
        urls: tuple[str, ...],
        limit: int,
    ) -> list[dict[str, Any]]:
        if not urls:
            raise ValueError("At least one iSlice URL must be configured")
        if limit < 1:
            return []
        normalized_urls = tuple(dict.fromkeys(url.rstrip("/") for url in urls))
        active_statuses = (
            "queued",
            "probing",
            "running",
            "pause_requested",
            "stop_requested",
        )
        active_placeholders = ", ".join("?" for _ in active_statuses)
        now = utc_now()
        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            active_rows = await (
                await db.execute(
                    f"""
                    SELECT j.islice_base_url, COUNT(*) AS job_count
                    FROM jobs j
                    WHERE j.status IN ({active_placeholders})
                      AND j.islice_base_url<>''
                      AND j.superseded_at IS NULL
                    GROUP BY j.islice_base_url
                    """,
                    active_statuses,
                )
            ).fetchall()
            candidates = await (
                await db.execute(
                    "SELECT * FROM jobs WHERE status='pending_schedule' "
                    "AND superseded_at IS NULL ORDER BY created_at, id"
                )
            ).fetchall()

            active_counts = {
                row["islice_base_url"]: int(row["job_count"])
                for row in active_rows
            }
            claimed: list[dict[str, Any]] = []
            for candidate_row in candidates:
                candidate = dict(candidate_row)
                assigned_url = str(candidate.get("islice_base_url") or "").rstrip("/")
                if assigned_url:
                    if assigned_url not in normalized_urls:
                        await db.execute(
                            "UPDATE jobs SET status='paused', error_message=?, updated_at=? WHERE id=?",
                            (
                                f"Job is assigned to unconfigured iSlice instance: {assigned_url}",
                                now,
                                candidate["id"],
                            ),
                        )
                        continue
                    selected_url = assigned_url
                else:
                    selected_url = min(
                        normalized_urls,
                        key=lambda url: active_counts.get(url, 0),
                    )

                await db.execute(
                    """
                    UPDATE jobs
                    SET status='queued', islice_base_url=?, error_message='', updated_at=?
                    WHERE id=? AND status='pending_schedule'
                    """,
                    (selected_url, now, candidate["id"]),
                )
                candidate["status"] = "queued"
                candidate["islice_base_url"] = selected_url
                candidate["error_message"] = ""
                candidate["updated_at"] = now
                claimed.append(candidate)
                active_counts[selected_url] = active_counts.get(selected_url, 0) + 1
                if len(claimed) >= limit:
                    break
            await db.commit()
        return claimed

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
        query += " WHERE j.superseded_at IS NULL"
        if status:
            query += " AND j.status=?"
            params.append(status)
        query += " GROUP BY j.id ORDER BY j.created_at DESC LIMIT ?"
        params.append(limit)
        async with self.connect() as db:
            rows = await (await db.execute(query, params)).fetchall()
        return [dict(row) for row in rows]

    async def paginate_jobs(
        self,
        *,
        page: int,
        page_size: int,
        status: str | None = None,
        channel_id: str | None = None,
        broadcast_date: str | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        where = ["j.superseded_at IS NULL"]
        params: list[Any] = []
        if status:
            where.append("j.status=?")
            params.append(status)
        if channel_id:
            where.append("j.channel_id=?")
            params.append(channel_id)
        if broadcast_date:
            where.append("j.broadcast_date=?")
            params.append(broadcast_date)
        where_sql = " AND ".join(where)
        async with self.connect() as db:
            total_row = await (
                await db.execute(
                    f"SELECT COUNT(*) AS value FROM jobs j WHERE {where_sql}", params
                )
            ).fetchone()
            rows = await (
                await db.execute(
                    f"""
                    SELECT j.*,
                           c.name AS channel_name,
                           (SELECT COUNT(*) FROM windows w WHERE w.job_id=j.id) AS window_count,
                           (SELECT COUNT(*) FROM segments s
                            WHERE s.job_id=j.id AND s.accepted=1) AS accepted_segment_count
                    FROM jobs j
                    LEFT JOIN channels c ON c.id=j.channel_id
                    WHERE {where_sql}
                    ORDER BY j.created_at DESC, j.id DESC
                    LIMIT ? OFFSET ?
                    """,
                    (*params, page_size, (page - 1) * page_size),
                )
            ).fetchall()
        return [dict(row) for row in rows], int(total_row["value"])

    async def get_channel_export(self, channel_id: str) -> dict[str, Any] | None:
        channel = await self.get_channel(channel_id)
        if channel is None:
            return None
        async with self.connect() as db:
            jobs = await (
                await db.execute(
                    "SELECT * FROM jobs WHERE channel_id=? AND superseded_at IS NULL "
                    "AND broadcast_date IS NOT NULL ORDER BY broadcast_date, created_at, id",
                    (channel_id,),
                )
            ).fetchall()
            segments = await (
                await db.execute(
                    """
                    SELECT s.*, w.window_index, j.broadcast_date, j.source_path,
                           j.source_url, j.created_at AS job_created_at
                    FROM segments s
                    JOIN windows w ON w.id=s.window_id
                    JOIN jobs j ON j.id=s.job_id
                    WHERE j.channel_id=? AND j.superseded_at IS NULL
                      AND j.broadcast_date IS NOT NULL AND s.accepted=1
                    ORDER BY j.broadcast_date,
                             CASE WHEN s.absolute_start IS NULL THEN 1 ELSE 0 END,
                             s.absolute_start, s.global_start, s.id
                    """,
                    (channel_id,),
                )
            ).fetchall()
        return {
            "channel": channel,
            "jobs": [dict(row) for row in jobs],
            "segments": [dict(row) for row in segments],
        }

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
                UPDATE jobs SET status='pending_schedule', updated_at=?
                WHERE status IN ('probing', 'running') AND superseded_at IS NULL
                """,
                (now,),
            )
            await db.execute(
                """
                UPDATE jobs SET status='paused', pause_requested=0, updated_at=?
                WHERE status='pause_requested' AND superseded_at IS NULL
                """,
                (now,),
            )
            await db.execute(
                """
                UPDATE jobs SET status='stopped', stop_requested=0, updated_at=?
                WHERE status='stop_requested' AND superseded_at IS NULL
                """,
                (now,),
            )
            await db.commit()

    async def recover_interrupted_resplits(self) -> None:
        message = "Helper restarted during manual resplit; start the resplit again"
        now = utc_now()
        async with self.connect() as db:
            rows = await (
                await db.execute(
                    """
                    SELECT DISTINCT w.id AS window_id, w.job_id
                    FROM attempts a
                    JOIN windows w ON w.id=a.window_id
                    WHERE a.status IN ('resplit_queued', 'resplitting')
                    """
                )
            ).fetchall()
            if not rows:
                return
            await db.execute(
                """
                UPDATE attempts
                SET status='failed', error_message=?, finished_at=?
                WHERE status IN ('resplit_queued', 'resplitting')
                """,
                (message, now),
            )
            for row in rows:
                await db.execute(
                    "UPDATE windows SET status='failed', error_message=?, updated_at=? WHERE id=?",
                    (message, now, row["window_id"]),
                )
                await db.execute(
                    """
                    UPDATE jobs
                    SET status='paused', pause_requested=0, error_message=?, updated_at=?
                    WHERE id=?
                    """,
                    (message, now, row["job_id"]),
                )
            await db.commit()

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
            "content_type",
            "news_event_type",
            "topic",
            "keywords_json",
            "summary",
            "segment_url",
            "cover_img_url",
            "attempt_id",
            "task_id",
            "raw_json",
        )
        async with self.connect() as db:
            attempt = await (
                await db.execute(
                    "SELECT id, task_id FROM attempts WHERE window_id=? "
                    "ORDER BY CASE WHEN status='completed' THEN 0 ELSE 1 END, "
                    "attempt_no DESC LIMIT 1",
                    (window_id,),
                )
            ).fetchone()
            await db.execute("DELETE FROM segments WHERE window_id=?", (window_id,))
            for segment in segments:
                row = {
                    **segment,
                    "job_id": job_id,
                    "window_id": window_id,
                    "attempt_id": attempt["id"] if attempt else None,
                    "task_id": attempt["task_id"] if attempt else "",
                }
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
