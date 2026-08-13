from __future__ import annotations

import hashlib
import json
import re
import unicodedata
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit

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


def absolute_time(program_start_time: str | None, offset_seconds: float) -> str | None:
    if not program_start_time:
        return None
    return (
        datetime.fromisoformat(program_start_time) + timedelta(seconds=offset_seconds)
    ).isoformat()


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
        "program_start_time",
        "time_reference_source",
        "time_reference_text",
        "time_reference_confidence",
        "time_reference_frame_path",
        "time_reference_frame_offset",
        "time_reference_error",
        "reviewed",
        "rebuild_revision",
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
        "submitted_at",
        "finished_at",
    }
    MERGE_FIELDS = {"title", "content_type", "ignored"}

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

                CREATE TABLE IF NOT EXISTS islice_instances (
                    id TEXT PRIMARY KEY,
                    source_id TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    base_url TEXT NOT NULL UNIQUE,
                    archive_catalog_url TEXT NOT NULL DEFAULT '',
                    schedulable INTEGER NOT NULL DEFAULT 1,
                    ssh_host TEXT NOT NULL DEFAULT '',
                    ssh_port INTEGER NOT NULL DEFAULT 22,
                    ssh_username TEXT NOT NULL DEFAULT 'root',
                    ssh_password_encrypted TEXT NOT NULL DEFAULT '',
                    ssh_host_key_sha256 TEXT NOT NULL DEFAULT '',
                    agent_install_path TEXT NOT NULL DEFAULT '',
                    islice_database_path TEXT NOT NULL DEFAULT '',
                    storage_root TEXT NOT NULL DEFAULT '',
                    archive_remote_host TEXT NOT NULL DEFAULT '',
                    archive_remote_user TEXT NOT NULL DEFAULT '',
                    archive_remote_root TEXT NOT NULL DEFAULT '',
                    archive_http_base TEXT NOT NULL DEFAULT '',
                    archive_ssh_key TEXT NOT NULL DEFAULT '',
                    archive_known_hosts TEXT NOT NULL DEFAULT '',
                    agent_status TEXT NOT NULL DEFAULT 'unconfigured',
                    agent_version TEXT NOT NULL DEFAULT '',
                    agent_last_checked_at TEXT,
                    agent_last_error TEXT NOT NULL DEFAULT '',
                    agent_deployed_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS system_reset_requests (
                    id TEXT PRIMARY KEY,
                    nonce TEXT NOT NULL,
                    confirmation_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    preview_json TEXT NOT NULL,
                    receipts_json TEXT NOT NULL DEFAULT '[]',
                    helper_backup_path TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    executed_at TEXT
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
                    time_reference_frame_offset REAL NOT NULL DEFAULT 0,
                    time_reference_error TEXT NOT NULL DEFAULT '',
                    cut_mode TEXT NOT NULL,
                    status TEXT NOT NULL,
                    progress REAL NOT NULL DEFAULT 0,
                    current_window INTEGER NOT NULL DEFAULT 0,
                    next_window_start REAL NOT NULL DEFAULT 0,
                    total_windows INTEGER NOT NULL,
                    pause_requested INTEGER NOT NULL DEFAULT 0,
                    stop_requested INTEGER NOT NULL DEFAULT 0,
                    reviewed INTEGER NOT NULL DEFAULT 0,
                    rebuild_revision INTEGER NOT NULL DEFAULT 0,
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
                    submitted_at TEXT NOT NULL DEFAULT '',
                    finished_at TEXT,
                    UNIQUE(window_id, attempt_no)
                );

                CREATE TABLE IF NOT EXISTS segments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                    window_id INTEGER NOT NULL REFERENCES windows(id) ON DELETE CASCADE,
                    source_index INTEGER NOT NULL,
                    accepted INTEGER NOT NULL,
                    ignored INTEGER NOT NULL DEFAULT 0,
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

                CREATE TABLE IF NOT EXISTS segment_merges (
                    id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                    primary_segment_id INTEGER REFERENCES segments(id) ON DELETE SET NULL,
                    status TEXT NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    content_type TEXT NOT NULL DEFAULT '',
                    news_event_type TEXT NOT NULL DEFAULT '',
                    topic TEXT NOT NULL DEFAULT '',
                    keywords_json TEXT NOT NULL DEFAULT '[]',
                    summary TEXT NOT NULL DEFAULT '',
                    global_start REAL NOT NULL,
                    global_end REAL NOT NULL,
                    ignored INTEGER NOT NULL DEFAULT 0,
                    member_count INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    cancelled_at TEXT,
                    cancellation_reason TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_segment_merges_job_status
                    ON segment_merges(job_id, status, global_start);

                CREATE TABLE IF NOT EXISTS segment_merge_members (
                    merge_id TEXT NOT NULL REFERENCES segment_merges(id) ON DELETE CASCADE,
                    segment_id INTEGER REFERENCES segments(id) ON DELETE SET NULL,
                    member_order INTEGER NOT NULL,
                    role TEXT NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    PRIMARY KEY(merge_id, member_order)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_segment_merge_member_active
                    ON segment_merge_members(segment_id)
                    WHERE active=1 AND segment_id IS NOT NULL;

                CREATE TABLE IF NOT EXISTS job_rebuilds (
                    id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                    start_window_index INTEGER NOT NULL,
                    generation INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    snapshot_path TEXT NOT NULL,
                    error_message TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    finished_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_job_rebuilds_job_created
                    ON job_rebuilds(job_id, created_at DESC);
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
                "time_reference_frame_offset": "REAL NOT NULL DEFAULT 0",
                "time_reference_error": "TEXT NOT NULL DEFAULT ''",
                "islice_base_url": "TEXT NOT NULL DEFAULT ''",
                "source_url": "TEXT NOT NULL DEFAULT ''",
                "channel_id": "TEXT REFERENCES channels(id)",
                "broadcast_date": "TEXT",
                "superseded_at": "TEXT",
                "superseded_by_job_id": "TEXT",
                "reviewed": "INTEGER NOT NULL DEFAULT 0",
                "rebuild_revision": "INTEGER NOT NULL DEFAULT 0",
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
                "submitted_at": "TEXT NOT NULL DEFAULT ''",
            }
            needs_submitted_at_backfill = "submitted_at" not in attempt_columns
            for column, declaration in attempt_migrations.items():
                if column not in attempt_columns:
                    await db.execute(f"ALTER TABLE attempts ADD COLUMN {column} {declaration}")
            if needs_submitted_at_backfill:
                await db.execute(
                    "UPDATE attempts SET submitted_at=created_at WHERE submitted_at=''"
                )
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
            if "ignored" not in segment_columns:
                await db.execute(
                    "ALTER TABLE segments ADD COLUMN ignored INTEGER NOT NULL DEFAULT 0"
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
            await db.execute(
                "INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES(12, ?)",
                (utc_now(),),
            )
            await db.execute(
                "INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES(13, ?)",
                (utc_now(),),
            )
            await db.execute(
                "INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES(14, ?)",
                (utc_now(),),
            )
            await db.execute(
                "INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES(15, ?)",
                (utc_now(),),
            )
            await db.execute(
                "INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES(16, ?)",
                (utc_now(),),
            )
            version_17 = await (
                await db.execute("SELECT 1 FROM schema_version WHERE version=17")
            ).fetchone()
            if version_17 is None:
                now = utc_now()
                interrupted = await (
                    await db.execute(
                        "SELECT DISTINCT job_id FROM job_rebuilds "
                        "WHERE status IN ('deleting', 'cleanup_failed')"
                    )
                ).fetchall()
                await db.execute(
                    "UPDATE job_rebuilds SET status='queued', error_message='', updated_at=? "
                    "WHERE status IN ('deleting', 'cleanup_failed')",
                    (now,),
                )
                for row in interrupted:
                    await db.execute(
                        "UPDATE jobs SET status='pending_schedule', error_message='', "
                        "updated_at=? WHERE id=?",
                        (now, row["job_id"]),
                    )
                await db.execute(
                    "INSERT INTO schema_version(version, applied_at) VALUES(17, ?)",
                    (now,),
                )
            await db.execute(
                "INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES(18, ?)",
                (utc_now(),),
            )
            await db.execute(
                "INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES(19, ?)",
                (utc_now(),),
            )
            await db.execute(
                "INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES(20, ?)",
                (utc_now(),),
            )
            instance_columns = {
                row["name"]
                for row in await (
                    await db.execute("PRAGMA table_info(islice_instances)")
                ).fetchall()
            }
            instance_migrations = {
                "ssh_host": "TEXT NOT NULL DEFAULT ''",
                "ssh_port": "INTEGER NOT NULL DEFAULT 22",
                "ssh_username": "TEXT NOT NULL DEFAULT 'root'",
                "ssh_password_encrypted": "TEXT NOT NULL DEFAULT ''",
                "ssh_host_key_sha256": "TEXT NOT NULL DEFAULT ''",
                "agent_install_path": "TEXT NOT NULL DEFAULT ''",
                "islice_database_path": "TEXT NOT NULL DEFAULT ''",
                "storage_root": "TEXT NOT NULL DEFAULT ''",
                "archive_remote_host": "TEXT NOT NULL DEFAULT ''",
                "archive_remote_user": "TEXT NOT NULL DEFAULT ''",
                "archive_remote_root": "TEXT NOT NULL DEFAULT ''",
                "archive_http_base": "TEXT NOT NULL DEFAULT ''",
                "archive_ssh_key": "TEXT NOT NULL DEFAULT ''",
                "archive_known_hosts": "TEXT NOT NULL DEFAULT ''",
                "agent_status": "TEXT NOT NULL DEFAULT 'unconfigured'",
                "agent_version": "TEXT NOT NULL DEFAULT ''",
                "agent_last_checked_at": "TEXT",
                "agent_last_error": "TEXT NOT NULL DEFAULT ''",
                "agent_deployed_at": "TEXT",
            }
            for name, definition in instance_migrations.items():
                if name not in instance_columns:
                    await db.execute(
                        f"ALTER TABLE islice_instances ADD COLUMN {name} {definition}"
                    )
            await db.execute(
                "INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES(21, ?)",
                (utc_now(),),
            )
            await db.commit()

    @staticmethod
    def _row(row: aiosqlite.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    async def seed_islice_instances(self, urls: Iterable[str]) -> None:
        now = utc_now()
        normalized = tuple(dict.fromkeys(url.rstrip("/") for url in urls if url))
        async with self.connect() as db:
            for index, url in enumerate(normalized, start=1):
                existing = await (
                    await db.execute(
                        "SELECT 1 FROM islice_instances WHERE base_url=?", (url,)
                    )
                ).fetchone()
                if existing:
                    continue
                host = (urlsplit(url).hostname or "").strip().lower()
                host_label = re.sub(r"[^a-z0-9._-]+", "-", host).strip("-._")
                if re.fullmatch(r"\d+(?:\.\d+){3}", host_label):
                    host_label = host_label.rsplit(".", 1)[-1]
                source_id = f"islice-{host_label or index}"[:64]
                base_source_id = source_id
                suffix = 1
                while await (
                    await db.execute(
                        "SELECT 1 FROM islice_instances WHERE source_id=?", (source_id,)
                    )
                ).fetchone():
                    suffix += 1
                    suffix_text = f"-{suffix}"
                    source_id = f"{base_source_id[:64 - len(suffix_text)]}{suffix_text}"
                await db.execute(
                    """
                    INSERT INTO islice_instances(
                        id,source_id,name,base_url,archive_catalog_url,
                        schedulable,created_at,updated_at
                    ) VALUES(?,?,?,?,'',1,?,?)
                    """,
                    (
                        uuid.uuid4().hex,
                        source_id,
                        f"iSlice {host or index}",
                        url,
                        now,
                        now,
                    ),
                )
            await db.commit()

    async def reset_preview_counts(self) -> dict[str, Any]:
        async with self.connect() as db:
            counts: dict[str, int] = {}
            for table in (
                "channels",
                "jobs",
                "windows",
                "attempts",
                "segments",
                "segment_merges",
                "segment_merge_members",
                "job_rebuilds",
            ):
                row = await (
                    await db.execute(f"SELECT COUNT(*) AS value FROM {table}")
                ).fetchone()
                counts[table] = int(row["value"])
            active_rows = await (
                await db.execute(
                    """
                    SELECT id,status,channel_name,broadcast_date FROM jobs
                    WHERE status IN (
                        'pending_schedule','queued','probing','running',
                        'pause_requested','stop_requested'
                    ) AND superseded_at IS NULL
                    ORDER BY created_at,id
                    """
                )
            ).fetchall()
        return {"counts": counts, "active_jobs": [dict(row) for row in active_rows]}

    async def create_system_reset_request(
        self,
        *,
        request_id: str,
        nonce: str,
        confirmation_hash: str,
        expires_at: str,
        preview: dict[str, Any],
    ) -> None:
        now = utc_now()
        async with self.connect() as db:
            await db.execute(
                "UPDATE system_reset_requests SET status='expired' "
                "WHERE status='prepared' AND expires_at<=?",
                (now,),
            )
            await db.execute(
                """
                INSERT INTO system_reset_requests(
                    id,nonce,confirmation_hash,status,expires_at,preview_json,created_at
                ) VALUES(?,?,?,'prepared',?,?,?)
                """,
                (
                    request_id,
                    nonce,
                    confirmation_hash,
                    expires_at,
                    json.dumps(preview, ensure_ascii=False),
                    now,
                ),
            )
            await db.commit()

    async def get_system_reset_request(self, request_id: str) -> dict[str, Any] | None:
        async with self.connect() as db:
            row = await (
                await db.execute(
                    "SELECT * FROM system_reset_requests WHERE id=?", (request_id,)
                )
            ).fetchone()
        return self._row(row)

    async def reset_operational_data(
        self,
        *,
        request_id: str,
        receipts: list[dict[str, Any]],
        helper_backup_path: str,
    ) -> dict[str, int]:
        now = utc_now()
        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            request_row = await (
                await db.execute(
                    "SELECT status,expires_at FROM system_reset_requests WHERE id=?",
                    (request_id,),
                )
            ).fetchone()
            if request_row is None or request_row["status"] != "prepared":
                await db.rollback()
                raise ValueError("Reset request is no longer prepared")
            if str(request_row["expires_at"]) <= now:
                await db.execute(
                    "UPDATE system_reset_requests SET status='expired' WHERE id=?",
                    (request_id,),
                )
                await db.commit()
                raise ValueError("Reset request has expired")
            active = await (
                await db.execute(
                    """
                    SELECT COUNT(*) AS value FROM jobs
                    WHERE status IN (
                        'pending_schedule','queued','probing','running',
                        'pause_requested','stop_requested'
                    ) AND superseded_at IS NULL
                    """
                )
            ).fetchone()
            if int(active["value"]):
                await db.rollback()
                raise ValueError("Active jobs exist")
            counts: dict[str, int] = {}
            for table in (
                "channels",
                "jobs",
                "windows",
                "attempts",
                "segments",
                "segment_merges",
                "segment_merge_members",
                "job_rebuilds",
            ):
                count_row = await (
                    await db.execute(f"SELECT COUNT(*) AS value FROM {table}")
                ).fetchone()
                counts[table] = int(count_row["value"])
            await db.execute("DELETE FROM jobs")
            await db.execute("DELETE FROM channels")
            await db.execute("UPDATE islice_instances SET schedulable=0,updated_at=?", (now,))
            await db.execute(
                "DELETE FROM sqlite_sequence WHERE name IN ('windows','attempts','segments')"
            )
            await db.execute(
                """
                UPDATE system_reset_requests
                SET status='helper_reset',receipts_json=?,helper_backup_path=?,executed_at=?
                WHERE id=?
                """,
                (
                    json.dumps(receipts, ensure_ascii=False),
                    helper_backup_path,
                    now,
                    request_id,
                ),
            )
            await db.commit()
        return counts

    async def list_islice_instances(self) -> list[dict[str, Any]]:
        async with self.connect() as db:
            rows = await (
                await db.execute(
                    """
                    SELECT i.*,
                           COUNT(DISTINCT CASE WHEN j.superseded_at IS NULL THEN j.id END)
                               AS job_count,
                           COUNT(DISTINCT CASE WHEN j.superseded_at IS NULL AND j.status IN (
                               'queued','probing','running','pause_requested','stop_requested'
                           ) THEN j.id END) AS active_job_count
                    FROM islice_instances i
                    LEFT JOIN jobs j ON j.islice_base_url=i.base_url
                    GROUP BY i.id ORDER BY i.source_id,i.id
                    """
                )
            ).fetchall()
        result = []
        for row in rows:
            item = self._public_islice_instance(dict(row))
            item["job_count"] = int(item["job_count"] or 0)
            item["active_job_count"] = int(item["active_job_count"] or 0)
            result.append(item)
        return result

    @staticmethod
    def _public_islice_instance(item: dict[str, Any]) -> dict[str, Any]:
        encrypted = str(item.pop("ssh_password_encrypted", "") or "")
        item["has_ssh_password"] = bool(encrypted)
        item["schedulable"] = bool(item["schedulable"])
        return item

    async def list_islice_instances_secret(self) -> list[dict[str, Any]]:
        async with self.connect() as db:
            rows = await (
                await db.execute("SELECT * FROM islice_instances ORDER BY source_id,id")
            ).fetchall()
        return [dict(row) for row in rows]

    async def get_islice_instance(self, instance_id: str) -> dict[str, Any] | None:
        async with self.connect() as db:
            row = await (
                await db.execute("SELECT * FROM islice_instances WHERE id=?", (instance_id,))
            ).fetchone()
        if row is None:
            return None
        return self._public_islice_instance(dict(row))

    async def get_islice_instance_secret(self, instance_id: str) -> dict[str, Any] | None:
        async with self.connect() as db:
            row = await (
                await db.execute("SELECT * FROM islice_instances WHERE id=?", (instance_id,))
            ).fetchone()
        return dict(row) if row else None

    async def create_islice_instance(self, record: dict[str, Any]) -> dict[str, Any]:
        now = utc_now()
        values = {
            "id": uuid.uuid4().hex,
            "source_id": record["source_id"],
            "name": record["name"],
            "base_url": record["base_url"].rstrip("/"),
            "archive_catalog_url": record.get("archive_catalog_url", "").rstrip("/"),
            "schedulable": int(bool(record.get("schedulable", True))),
            "ssh_host": record.get("ssh_host", ""),
            "ssh_port": int(record.get("ssh_port", 22)),
            "ssh_username": record.get("ssh_username", "root"),
            "ssh_password_encrypted": record.get("ssh_password_encrypted", ""),
            "agent_install_path": record.get("agent_install_path", ""),
            "islice_database_path": record.get("islice_database_path", ""),
            "storage_root": record.get("storage_root", ""),
            "archive_remote_host": record.get("archive_remote_host", ""),
            "archive_remote_user": record.get("archive_remote_user", ""),
            "archive_remote_root": record.get("archive_remote_root", ""),
            "archive_http_base": record.get("archive_http_base", ""),
            "archive_ssh_key": record.get("archive_ssh_key", ""),
            "archive_known_hosts": record.get("archive_known_hosts", ""),
            "agent_status": "unconfigured",
            "created_at": now,
            "updated_at": now,
        }
        async with self.connect() as db:
            row = await (
                await db.execute(
                    """
                    INSERT INTO islice_instances(
                        id,source_id,name,base_url,archive_catalog_url,
                        schedulable,ssh_host,ssh_port,ssh_username,
                        ssh_password_encrypted,agent_install_path,islice_database_path,
                        storage_root,archive_remote_host,archive_remote_user,
                        archive_remote_root,archive_http_base,archive_ssh_key,
                        archive_known_hosts,agent_status,created_at,updated_at
                    ) VALUES(:id,:source_id,:name,:base_url,:archive_catalog_url,
                             :schedulable,:ssh_host,:ssh_port,:ssh_username,
                             :ssh_password_encrypted,:agent_install_path,
                             :islice_database_path,:storage_root,:archive_remote_host,
                             :archive_remote_user,:archive_remote_root,:archive_http_base,
                             :archive_ssh_key,:archive_known_hosts,:agent_status,
                             :created_at,:updated_at) RETURNING *
                    """,
                    values,
                )
            ).fetchone()
            await db.commit()
        return self._public_islice_instance(dict(row))

    async def update_islice_instance(
        self, instance_id: str, record: dict[str, Any]
    ) -> dict[str, Any] | None:
        current = await self.get_islice_instance_secret(instance_id)
        if current is None:
            return None
        old_url = str(current["base_url"])
        new_url = str(record["base_url"]).rstrip("/")
        source_id_changed = str(current["source_id"]) != str(record["source_id"])
        ssh_endpoint_changed = (
            str(current.get("ssh_host") or "") != str(record.get("ssh_host") or "")
            or int(current.get("ssh_port") or 22) != int(record.get("ssh_port") or 22)
        )
        deploy_config_changed = any(
            str(current.get(key) or "") != str(record.get(key) or "")
            for key in (
                "ssh_host", "ssh_port", "ssh_username", "agent_install_path",
                "islice_database_path", "storage_root", "archive_remote_host",
                "archive_remote_user", "archive_remote_root", "archive_http_base",
                "archive_ssh_key", "archive_known_hosts",
            )
        ) or "ssh_password_encrypted" in record
        if old_url != new_url or source_id_changed:
            async with self.connect() as db:
                used = await (
                    await db.execute(
                        "SELECT 1 FROM jobs WHERE islice_base_url=? LIMIT 1", (old_url,)
                    )
                ).fetchone()
            if used:
                if old_url != new_url:
                    raise ValueError(
                        "Cannot change the address of an instance already used by jobs"
                    )
                raise ValueError(
                    "Cannot change sourceId of an instance already used by jobs"
                )
        fields = {
            "source_id": record["source_id"],
            "name": record["name"],
            "base_url": new_url,
            "archive_catalog_url": record.get("archive_catalog_url", "").rstrip("/"),
            "schedulable": int(bool(record.get("schedulable", True))),
            "ssh_host": record.get("ssh_host", ""),
            "ssh_port": int(record.get("ssh_port", 22)),
            "ssh_username": record.get("ssh_username", "root"),
            "ssh_password_encrypted": record.get(
                "ssh_password_encrypted", current.get("ssh_password_encrypted", "")
            ),
            "agent_install_path": record.get("agent_install_path", ""),
            "islice_database_path": record.get("islice_database_path", ""),
            "storage_root": record.get("storage_root", ""),
            "archive_remote_host": record.get("archive_remote_host", ""),
            "archive_remote_user": record.get("archive_remote_user", ""),
            "archive_remote_root": record.get("archive_remote_root", ""),
            "archive_http_base": record.get("archive_http_base", ""),
            "archive_ssh_key": record.get("archive_ssh_key", ""),
            "archive_known_hosts": record.get("archive_known_hosts", ""),
            "ssh_host_key_sha256": (
                "" if ssh_endpoint_changed else current.get("ssh_host_key_sha256", "")
            ),
            "agent_status": (
                "unconfigured" if deploy_config_changed else current.get("agent_status", "unconfigured")
            ),
            "agent_last_error": "" if deploy_config_changed else current.get("agent_last_error", ""),
            "updated_at": utc_now(),
            "id": instance_id,
        }
        async with self.connect() as db:
            row = await (
                await db.execute(
                    """
                    UPDATE islice_instances SET source_id=:source_id,name=:name,
                        base_url=:base_url,archive_catalog_url=:archive_catalog_url,
                        schedulable=:schedulable,ssh_host=:ssh_host,ssh_port=:ssh_port,
                        ssh_username=:ssh_username,
                        ssh_password_encrypted=:ssh_password_encrypted,
                        agent_install_path=:agent_install_path,
                        islice_database_path=:islice_database_path,
                        storage_root=:storage_root,
                        archive_remote_host=:archive_remote_host,
                        archive_remote_user=:archive_remote_user,
                        archive_remote_root=:archive_remote_root,
                        archive_http_base=:archive_http_base,
                        archive_ssh_key=:archive_ssh_key,
                        archive_known_hosts=:archive_known_hosts,
                        ssh_host_key_sha256=:ssh_host_key_sha256,
                        agent_status=:agent_status,
                        agent_last_error=:agent_last_error,
                        updated_at=:updated_at
                    WHERE id=:id RETURNING *
                    """,
                    fields,
                )
            ).fetchone()
            await db.commit()
        return self._public_islice_instance(dict(row))

    async def update_agent_health(
        self,
        instance_id: str,
        *,
        status: str,
        version: str | None = None,
        error: str = "",
        host_key: str = "",
        deployed: bool = False,
    ) -> None:
        now = utc_now()
        assignments = [
            "agent_status=?", "agent_last_checked_at=?", "agent_last_error=?",
            "updated_at=?",
        ]
        values: list[Any] = [status, now, error, now]
        if version is not None:
            assignments.append("agent_version=?")
            values.append(version)
        if host_key:
            assignments.append("ssh_host_key_sha256=?")
            values.append(host_key)
        if deployed:
            assignments.append("agent_deployed_at=?")
            values.append(now)
        values.append(instance_id)
        async with self.connect() as db:
            await db.execute(
                f"UPDATE islice_instances SET {', '.join(assignments)} WHERE id=?",
                values,
            )
            await db.commit()

    async def delete_islice_instance(self, instance_id: str) -> bool:
        current = await self.get_islice_instance(instance_id)
        if current is None:
            return False
        async with self.connect() as db:
            used = await (
                await db.execute(
                    "SELECT 1 FROM jobs WHERE islice_base_url=? LIMIT 1",
                    (current["base_url"],),
                )
            ).fetchone()
            if used:
                raise ValueError("Cannot delete an instance already used by jobs")
            cursor = await db.execute(
                "DELETE FROM islice_instances WHERE id=?", (instance_id,)
            )
            await db.commit()
        return bool(cursor.rowcount)

    async def schedulable_islice_urls(self) -> tuple[str, ...]:
        async with self.connect() as db:
            rows = await (
                await db.execute(
                    "SELECT base_url FROM islice_instances WHERE schedulable=1 ORDER BY source_id,id"
                )
            ).fetchall()
        return tuple(str(row["base_url"]).rstrip("/") for row in rows)

    async def all_islice_urls(self) -> tuple[str, ...]:
        async with self.connect() as db:
            rows = await (
                await db.execute(
                    "SELECT base_url FROM islice_instances ORDER BY source_id,id"
                )
            ).fetchall()
        return tuple(str(row["base_url"]).rstrip("/") for row in rows)

    async def archive_task_contexts(self) -> dict[tuple[str, str], dict[str, Any]]:
        async with self.connect() as db:
            rows = await (
                await db.execute(
                    """
                    SELECT i.source_id,a.task_id,j.id AS job_id,j.channel_id,
                           j.channel_name,j.broadcast_date,j.program_start_time,
                           j.status AS job_status,j.islice_base_url,
                           w.window_index,w.requested_start,a.status AS attempt_status,
                           SUM(CASE WHEN s.accepted=1 AND s.ignored=0 THEN 1 ELSE 0 END)
                               AS accepted_segment_count
                    FROM attempts a
                    JOIN windows w ON w.id=a.window_id
                    JOIN jobs j ON j.id=w.job_id
                    LEFT JOIN islice_instances i ON i.base_url=j.islice_base_url
                    LEFT JOIN segments s ON s.attempt_id=a.id
                    GROUP BY a.id
                    """
                )
            ).fetchall()
        return {
            (str(row["source_id"] or ""), str(row["task_id"])): dict(row)
            for row in rows
        }

    async def archive_references(self, task_id: str) -> list[dict[str, Any]]:
        async with self.connect() as db:
            rows = await (
                await db.execute(
                    """
                    SELECT s.task_id,s.segment_url,s.cover_img_url,w.window_index,
                           j.id AS job_id,j.islice_base_url,a.status AS attempt_status
                    FROM segments s
                    JOIN windows w ON w.id=s.window_id
                    JOIN jobs j ON j.id=s.job_id
                    LEFT JOIN attempts a ON a.id=s.attempt_id
                    WHERE s.task_id=? AND s.accepted=1 AND s.ignored=0
                    ORDER BY j.id,w.window_index,s.global_start,s.id
                    """,
                    (task_id,),
                )
            ).fetchall()
        return [dict(row) for row in rows]

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
            "error_message": record.get("error_message", ""),
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
        configured_urls: tuple[str, ...] | None = None,
    ) -> list[dict[str, Any]]:
        if limit < 1:
            return []
        normalized_urls = tuple(dict.fromkeys(url.rstrip("/") for url in urls))
        normalized_configured = tuple(
            dict.fromkeys(
                url.rstrip("/") for url in (configured_urls or normalized_urls)
            )
        )
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
                    "AND superseded_at IS NULL "
                    "ORDER BY current_window ASC, created_at ASC, id ASC"
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
                    if assigned_url not in normalized_configured:
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
                    if not normalized_urls:
                        continue
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
                   COUNT(DISTINCT CASE WHEN s.accepted=1 AND s.ignored=0
                         AND NOT EXISTS(
                             SELECT 1 FROM segment_merge_members mm
                             WHERE mm.segment_id=s.id AND mm.active=1
                         ) THEN s.id END)
                   + (SELECT COUNT(*) FROM segment_merges sm
                      WHERE sm.job_id=j.id AND sm.status='active' AND sm.ignored=0)
                     AS accepted_segment_count
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
        islice_base_url: str | None = None,
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
        if islice_base_url:
            where.append("j.islice_base_url=?")
            params.append(islice_base_url.rstrip("/"))
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
                            WHERE s.job_id=j.id AND s.accepted=1 AND s.ignored=0
                              AND NOT EXISTS(
                                  SELECT 1 FROM segment_merge_members mm
                                  WHERE mm.segment_id=s.id AND mm.active=1
                              ))
                           + (SELECT COUNT(*) FROM segment_merges sm
                              WHERE sm.job_id=j.id AND sm.status='active' AND sm.ignored=0)
                             AS accepted_segment_count
                           ,(SELECT w.window_index
                             FROM windows w
                             WHERE w.job_id=j.id AND w.window_index=j.current_window
                             LIMIT 1) AS current_task_window
                           ,(SELECT a.task_id
                             FROM attempts a JOIN windows w ON w.id=a.window_id
                             WHERE w.job_id=j.id AND w.window_index=j.current_window
                             ORDER BY a.attempt_no DESC LIMIT 1) AS current_task_id
                           ,(SELECT a.status
                             FROM attempts a JOIN windows w ON w.id=a.window_id
                             WHERE w.job_id=j.id AND w.window_index=j.current_window
                             ORDER BY a.attempt_no DESC LIMIT 1) AS current_task_status
                           ,(SELECT a.service_status
                             FROM attempts a JOIN windows w ON w.id=a.window_id
                             WHERE w.job_id=j.id AND w.window_index=j.current_window
                             ORDER BY a.attempt_no DESC LIMIT 1) AS current_task_service_status
                           ,(SELECT a.progress
                             FROM attempts a JOIN windows w ON w.id=a.window_id
                             WHERE w.job_id=j.id AND w.window_index=j.current_window
                             ORDER BY a.attempt_no DESC LIMIT 1) AS current_task_progress
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
                      AND j.broadcast_date IS NOT NULL AND s.accepted=1 AND s.ignored=0
                      AND NOT EXISTS(
                          SELECT 1 FROM segment_merge_members mm
                          WHERE mm.segment_id=s.id AND mm.active=1
                      )
                    ORDER BY j.broadcast_date,
                             CASE WHEN s.absolute_start IS NULL THEN 1 ELSE 0 END,
                             s.absolute_start, s.global_start, s.id
                    """,
                    (channel_id,),
                )
            ).fetchall()
            merge_rows = await (
                await db.execute(
                    """
                    SELECT sm.*, j.broadcast_date, j.source_path, j.source_url,
                           j.created_at AS job_created_at, j.program_start_time
                    FROM segment_merges sm
                    JOIN jobs j ON j.id=sm.job_id
                    WHERE j.channel_id=? AND j.superseded_at IS NULL
                      AND j.broadcast_date IS NOT NULL
                      AND sm.status='active' AND sm.ignored=0
                    """,
                    (channel_id,),
                )
            ).fetchall()
        effective_segments = [
            {**dict(row), "manual_merge": 0, "merge_id": None}
            for row in segments
        ]
        for stored in merge_rows:
            merge = dict(stored)
            effective_segments.append(
                {
                    **merge,
                    "id": f"merge:{merge['id']}",
                    "absolute_start": absolute_time(
                        merge["program_start_time"], float(merge["global_start"])
                    ),
                    "absolute_end": absolute_time(
                        merge["program_start_time"], float(merge["global_end"])
                    ),
                    "accepted": 1,
                    "manual_merge": 1,
                    "merge_id": merge["id"],
                }
            )
        effective_segments.sort(
            key=lambda row: (
                str(row["broadcast_date"]),
                1 if row.get("absolute_start") is None else 0,
                str(row.get("absolute_start") or ""),
                float(row["global_start"]),
                str(row["id"]),
            )
        )
        return {
            "channel": channel,
            "jobs": [dict(row) for row in jobs],
            "segments": effective_segments,
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

    async def update_time_reference(
        self,
        job_id: str,
        program_start_time: str,
        *,
        source: str = "manual_override",
        reference_text: str | None = None,
        reference_confidence: float | None = None,
        reference_frame_path: str | None = None,
        reference_frame_offset: float | None = None,
        reference_error: str | None = None,
        warning: str | None = None,
    ) -> tuple[dict[str, Any] | None, int]:
        """Update the base time and all derived segment timestamps atomically."""
        now = utc_now()
        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            job = await (
                await db.execute("SELECT * FROM jobs WHERE id=?", (job_id,))
            ).fetchone()
            if job is None:
                await db.rollback()
                return None, 0

            warnings = json.loads(job["warnings_json"] or "[]")
            previous = job["program_start_time"] or "unavailable"
            warnings.append(
                warning
                or f"Time reference manually changed from {previous} to {program_start_time}"
            )
            fields: dict[str, Any] = {
                "program_start_time": program_start_time,
                "time_reference_source": source,
                "warnings_json": json.dumps(warnings, ensure_ascii=False),
                "updated_at": now,
            }
            optional_fields = {
                "time_reference_text": reference_text,
                "time_reference_confidence": reference_confidence,
                "time_reference_frame_path": reference_frame_path,
                "time_reference_frame_offset": reference_frame_offset,
                "time_reference_error": reference_error,
            }
            fields.update(
                {
                    key: value
                    for key, value in optional_fields.items()
                    if value is not None
                }
            )
            # A creation-time stop caused by a missing reference becomes
            # resumable as soon as the operator supplies the missing value.
            if (
                job["status"] == "stopped"
                and not job["program_start_time"]
                and int(job["current_window"] or 0) == 0
                and not job["started_at"]
            ):
                fields.update(
                    status="paused",
                    error_message="",
                    stop_requested=0,
                    completed_at=None,
                )

            assignments = ", ".join(f"{key}=?" for key in fields)
            await db.execute(
                f"UPDATE jobs SET {assignments} WHERE id=?",
                (*fields.values(), job_id),
            )
            segments = await (
                await db.execute(
                    "SELECT id, global_start, global_end FROM segments WHERE job_id=?",
                    (job_id,),
                )
            ).fetchall()
            await db.executemany(
                "UPDATE segments SET absolute_start=?, absolute_end=? WHERE id=?",
                [
                    (
                        absolute_time(program_start_time, float(row["global_start"])),
                        absolute_time(program_start_time, float(row["global_end"])),
                        row["id"],
                    )
                    for row in segments
                ],
            )
            updated = await (
                await db.execute("SELECT * FROM jobs WHERE id=?", (job_id,))
            ).fetchone()
            await db.commit()
        return self._row(updated), len(segments)

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

    async def recover_interrupted_resplits(self) -> list[dict[str, Any]]:
        """Return resplits that were in progress so the orchestrator can resume them.

        Resplit execution is deliberately driven by the persisted attempt row.  A
        process restart must not turn a recoverable operation into a failure.
        """
        async with self.connect() as db:
            rows = await (
                await db.execute(
                    """
                    SELECT a.id AS attempt_id, a.window_id, a.task_id,
                           w.job_id, w.window_index, j.islice_base_url
                    FROM attempts a
                    JOIN windows w ON w.id=a.window_id
                    JOIN jobs j ON j.id=w.job_id
                    WHERE a.status IN ('resplit_queued', 'resplitting')
                    ORDER BY a.created_at, a.id
                    """
                )
            ).fetchall()
        return [dict(row) for row in rows]

    async def paused_jobs_with_polling_attempts(self) -> list[dict[str, Any]]:
        """Return submitted current-window attempts that must drain while paused."""
        async with self.connect() as db:
            rows = await (
                await db.execute(
                    """
                    SELECT j.id AS job_id, w.id AS window_id, w.window_index,
                           a.id AS attempt_id, a.task_id
                    FROM jobs j
                    JOIN windows w
                      ON w.job_id=j.id AND w.window_index=j.current_window
                    JOIN attempts a ON a.window_id=w.id
                    WHERE j.status='paused'
                      AND j.superseded_at IS NULL
                      AND w.status='polling'
                      AND a.status='polling'
                      AND COALESCE(a.submitted_at, '')<>''
                      AND a.attempt_no=(
                          SELECT MAX(a2.attempt_no)
                          FROM attempts a2 WHERE a2.window_id=w.id
                      )
                    ORDER BY j.created_at, j.id
                    """
                )
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _tail_token(
        job: dict[str, Any],
        windows: list[dict[str, Any]],
        attempts: list[dict[str, Any]],
        segments: list[dict[str, Any]],
        merges: list[dict[str, Any]],
        merge_members: list[dict[str, Any]],
    ) -> str:
        payload = {
            "job": {
                key: job.get(key)
                for key in (
                    "id", "status", "current_window", "next_window_start",
                    "total_windows", "rebuild_revision", "source_size",
                    "source_mtime_ns", "updated_at",
                )
            },
            "windows": windows,
            "attempts": attempts,
            "segments": segments,
            "merges": merges,
            "merge_members": merge_members,
        }
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    async def _tail_state(
        self, db: aiosqlite.Connection, job_id: str, start_window_index: int
    ) -> dict[str, Any] | None:
        job_row = await (
            await db.execute("SELECT * FROM jobs WHERE id=?", (job_id,))
        ).fetchone()
        if job_row is None:
            return None
        window_rows = await (
            await db.execute(
                "SELECT * FROM windows WHERE job_id=? AND window_index>=? "
                "ORDER BY window_index",
                (job_id, start_window_index),
            )
        ).fetchall()
        windows = [dict(row) for row in window_rows]
        if not windows or int(windows[0]["window_index"]) != start_window_index:
            raise ValueError("Window not found")
        window_ids = [int(row["id"]) for row in windows]
        placeholders = ", ".join("?" for _ in window_ids)
        attempt_rows = await (
            await db.execute(
                f"SELECT * FROM attempts WHERE window_id IN ({placeholders}) "
                "ORDER BY window_id, attempt_no",
                window_ids,
            )
        ).fetchall()
        segment_rows = await (
            await db.execute(
                f"SELECT * FROM segments WHERE window_id IN ({placeholders}) "
                "ORDER BY window_id, source_index",
                window_ids,
            )
        ).fetchall()
        merge_rows = await (
            await db.execute(
                f"""
                SELECT DISTINCT sm.*
                FROM segment_merges sm
                JOIN segment_merge_members mm ON mm.merge_id=sm.id
                JOIN segments s ON s.id=mm.segment_id
                WHERE s.window_id IN ({placeholders})
                  AND sm.status='active' AND mm.active=1
                ORDER BY sm.created_at, sm.id
                """,
                window_ids,
            )
        ).fetchall()
        merges = [dict(row) for row in merge_rows]
        merge_members: list[dict[str, Any]] = []
        if merges:
            merge_ids = [row["id"] for row in merges]
            merge_placeholders = ", ".join("?" for _ in merge_ids)
            member_rows = await (
                await db.execute(
                    f"SELECT * FROM segment_merge_members "
                    f"WHERE merge_id IN ({merge_placeholders}) "
                    "ORDER BY merge_id, member_order",
                    merge_ids,
                )
            ).fetchall()
            merge_members = [dict(row) for row in member_rows]
        job = dict(job_row)
        attempts = [dict(row) for row in attempt_rows]
        segments = [dict(row) for row in segment_rows]
        previous = None
        if start_window_index > 0:
            previous_row = await (
                await db.execute(
                    "SELECT * FROM windows WHERE job_id=? AND window_index=?",
                    (job_id, start_window_index - 1),
                )
            ).fetchone()
            previous = dict(previous_row) if previous_row else None
        return {
            "job": job,
            "windows": windows,
            "attempts": attempts,
            "segments": segments,
            "merges": merges,
            "merge_members": merge_members,
            "previous_window": previous,
            "preview_token": self._tail_token(
                job, windows, attempts, segments, merges, merge_members
            ),
        }

    async def get_rebuild_preview(
        self, job_id: str, start_window_index: int
    ) -> dict[str, Any] | None:
        async with self.connect() as db:
            return await self._tail_state(db, job_id, start_window_index)

    async def truncate_job_for_rebuild(
        self,
        job_id: str,
        start_window_index: int,
        expected_token: str,
        snapshot_path: str,
    ) -> dict[str, Any]:
        now = utc_now()
        rebuild_id = uuid.uuid4().hex
        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            state = await self._tail_state(db, job_id, start_window_index)
            if state is None:
                await db.rollback()
                raise ValueError("Job not found")
            if state["preview_token"] != expected_token:
                await db.rollback()
                raise ValueError("The job changed after preview; preview it again")
            job = state["job"]
            if job["status"] not in {"paused", "failed", "stopped", "completed"}:
                await db.rollback()
                raise ValueError("Pause or finish the job before rebuilding its tail")
            previous = state["previous_window"]
            if start_window_index > 0 and (
                previous is None or previous["status"] != "completed"
            ):
                await db.rollback()
                raise ValueError("The window before the rebuild point is not completed")

            generation = int(job.get("rebuild_revision") or 0) + 1
            next_window_start = 0.0
            if previous is not None:
                next_window_start = float(
                    previous["handoff_start"]
                    if previous["handoff_start"] is not None
                    else previous["nominal_end"]
                )
            await db.execute(
                """
                INSERT INTO job_rebuilds(
                    id, job_id, start_window_index, generation, status,
                    snapshot_path, error_message, created_at, updated_at
                ) VALUES(?, ?, ?, ?, 'queued', ?, '', ?, ?)
                """,
                (
                    rebuild_id, job_id, start_window_index, generation,
                    snapshot_path, now, now,
                ),
            )
            for merge in state["merges"]:
                await db.execute(
                    "UPDATE segment_merges SET status='invalidated', cancelled_at=?, "
                    "updated_at=?, cancellation_reason='tail rebuild removed a member' "
                    "WHERE id=? AND status='active'",
                    (now, now, merge["id"]),
                )
                await db.execute(
                    "UPDATE segment_merge_members SET active=0 WHERE merge_id=?",
                    (merge["id"],),
                )
            await db.execute(
                "DELETE FROM windows WHERE job_id=? AND window_index>=?",
                (job_id, start_window_index),
            )
            await db.execute(
                """
                UPDATE jobs SET status='pending_schedule', current_window=?, next_window_start=?,
                    progress=?, pause_requested=0, stop_requested=0, reviewed=0,
                    error_message='', completed_at=NULL, rebuild_revision=?, updated_at=?
                WHERE id=?
                """,
                (
                    start_window_index,
                    next_window_start,
                    min(100.0, start_window_index / int(job["total_windows"]) * 100.0),
                    generation,
                    now,
                    job_id,
                ),
            )
            row = await (
                await db.execute("SELECT * FROM job_rebuilds WHERE id=?", (rebuild_id,))
            ).fetchone()
            await db.commit()
        return dict(row)

    async def get_latest_job_rebuild(self, job_id: str) -> dict[str, Any] | None:
        async with self.connect() as db:
            row = await (
                await db.execute(
                    "SELECT * FROM job_rebuilds WHERE job_id=? "
                    "ORDER BY created_at DESC, id DESC LIMIT 1",
                    (job_id,),
                )
            ).fetchone()
        return self._row(row)

    async def mark_latest_rebuild_completed(self, job_id: str) -> None:
        now = utc_now()
        async with self.connect() as db:
            await db.execute(
                "UPDATE job_rebuilds SET status='completed', finished_at=?, updated_at=? "
                "WHERE id=(SELECT id FROM job_rebuilds WHERE job_id=? AND status='queued' "
                "ORDER BY created_at DESC, id DESC LIMIT 1)",
                (now, now, job_id),
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

    async def replace_attempt_for_resplit(
        self,
        window_id: int,
        expected_attempt_id: int,
        attempt_no: int,
        task_id: str,
    ) -> dict[str, Any] | None:
        """Replace the latest local attempt without deleting its iSlice task."""
        now = utc_now()
        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            latest = await (
                await db.execute(
                    "SELECT id FROM attempts WHERE window_id=? "
                    "ORDER BY attempt_no DESC LIMIT 1",
                    (window_id,),
                )
            ).fetchone()
            if latest is None or int(latest["id"]) != expected_attempt_id:
                await db.rollback()
                return None
            await db.execute(
                "UPDATE segments SET attempt_id=NULL WHERE attempt_id=?",
                (expected_attempt_id,),
            )
            await db.execute(
                "DELETE FROM attempts WHERE id=?", (expected_attempt_id,)
            )
            row = await (
                await db.execute(
                    """
                    INSERT INTO attempts(
                        window_id, attempt_no, task_id, status, service_status,
                        progress, raw_response_path, error_message, created_at,
                        submitted_at, finished_at
                    ) VALUES(?, ?, ?, 'resplit_queued', 'waiting', 0, '', '', ?, '', NULL)
                    RETURNING *
                    """,
                    (window_id, attempt_no, task_id, now),
                )
            ).fetchone()
            await db.commit()
        return dict(row) if row is not None else None

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
            await db.execute("BEGIN IMMEDIATE")
            job = await (
                await db.execute(
                    "SELECT program_start_time FROM jobs WHERE id=?", (job_id,)
                )
            ).fetchone()
            program_start_time = job["program_start_time"] if job else None
            attempt = await (
                await db.execute(
                    "SELECT id, task_id FROM attempts WHERE window_id=? "
                    "ORDER BY CASE WHEN status='completed' THEN 0 ELSE 1 END, "
                    "attempt_no DESC LIMIT 1",
                    (window_id,),
                )
            ).fetchone()
            affected_merges = await (
                await db.execute(
                    """
                    SELECT DISTINCT mm.merge_id
                    FROM segment_merge_members mm
                    JOIN segments s ON s.id=mm.segment_id
                    JOIN segment_merges sm ON sm.id=mm.merge_id
                    WHERE s.window_id=? AND mm.active=1 AND sm.status='active'
                    """,
                    (window_id,),
                )
            ).fetchall()
            now = utc_now()
            for merge in affected_merges:
                await db.execute(
                    "UPDATE segment_merges SET status='invalidated', cancelled_at=?, "
                    "updated_at=?, cancellation_reason='window resplit replaced a member' "
                    "WHERE id=?",
                    (now, now, merge["merge_id"]),
                )
                await db.execute(
                    "UPDATE segment_merge_members SET active=0 WHERE merge_id=?",
                    (merge["merge_id"],),
                )
            if affected_merges:
                await db.execute(
                    "UPDATE jobs SET reviewed=0, updated_at=? WHERE id=?",
                    (now, job_id),
                )
            await db.execute("DELETE FROM segments WHERE window_id=?", (window_id,))
            for segment in segments:
                row = {
                    **segment,
                    "job_id": job_id,
                    "window_id": window_id,
                    "absolute_start": absolute_time(
                        program_start_time, float(segment["global_start"])
                    ),
                    "absolute_end": absolute_time(
                        program_start_time, float(segment["global_end"])
                    ),
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
            SELECT s.*, w.window_index, j.islice_base_url,
                   mm.merge_id AS active_merge_id,
                   mm.member_order AS merge_member_order,
                   mm.role AS merge_role,
                   sm.primary_segment_id AS merge_primary_segment_id,
                   sm.title AS active_merge_title,
                   sm.member_count AS active_merge_member_count,
                   sm.global_start AS active_merge_global_start
            FROM segments s
            JOIN windows w ON w.id=s.window_id
            JOIN jobs j ON j.id=s.job_id
            LEFT JOIN segment_merge_members mm
              ON mm.segment_id=s.id AND mm.active=1
            LEFT JOIN segment_merges sm
              ON sm.id=mm.merge_id AND sm.status='active'
            WHERE s.job_id=?
        """
        params: list[Any] = [job_id]
        if accepted_only:
            query += " AND s.accepted=1"
        async with self.connect() as db:
            rows = await (await db.execute(query, params)).fetchall()
            merge_rows = await (
                await db.execute(
                    "SELECT * FROM segment_merges WHERE job_id=? AND status='active' "
                    "ORDER BY global_start, id",
                    (job_id,),
                )
            ).fetchall()
            job = await (
                await db.execute(
                    "SELECT program_start_time FROM jobs WHERE id=?", (job_id,)
                )
            ).fetchone()

        raw = []
        members_by_merge: dict[str, list[dict[str, Any]]] = {}
        for stored in rows:
            row = dict(stored)
            row["record_kind"] = "segment"
            row["manual_merge"] = 0
            raw.append(row)
            if row.get("active_merge_id"):
                members_by_merge.setdefault(str(row["active_merge_id"]), []).append(row)

        program_start_time = job["program_start_time"] if job else None
        merges: list[dict[str, Any]] = []
        merge_starts: dict[str, float] = {}
        for stored in merge_rows:
            merge = dict(stored)
            merge_id = str(merge["id"])
            members = sorted(
                members_by_merge.get(merge_id, []),
                key=lambda item: int(item["merge_member_order"]),
            )
            primary = next(
                (item for item in members if item.get("merge_role") == "primary"),
                members[0] if members else None,
            )
            merge_starts[merge_id] = float(merge["global_start"])
            merges.append(
                {
                    "id": f"merge:{merge_id}",
                    "merge_id": merge_id,
                    "job_id": job_id,
                    "window_id": members[0]["window_id"] if members else None,
                    "window_index": min(
                        (int(item["window_index"]) for item in members), default=0
                    ),
                    "accepted": 1,
                    "ignored": int(merge["ignored"]),
                    "reason": "manual merge",
                    "local_start": 0.0,
                    "local_end": float(merge["global_end"]) - float(merge["global_start"]),
                    "global_start": float(merge["global_start"]),
                    "global_end": float(merge["global_end"]),
                    "absolute_start": absolute_time(
                        program_start_time, float(merge["global_start"])
                    ),
                    "absolute_end": absolute_time(
                        program_start_time, float(merge["global_end"])
                    ),
                    "title": merge["title"],
                    "content_type": merge["content_type"],
                    "news_event_type": merge["news_event_type"],
                    "topic": merge["topic"],
                    "keywords_json": merge["keywords_json"],
                    "summary": merge["summary"],
                    "segment_url": "",
                    "cover_img_url": "",
                    "attempt_id": None,
                    "task_id": primary.get("task_id", "") if primary else "",
                    "islice_base_url": primary.get("islice_base_url", "") if primary else "",
                    "source_index": primary.get("source_index", -1) if primary else -1,
                    "raw_json": "{}",
                    "record_kind": "merge",
                    "manual_merge": 1,
                    "primary_segment_id": merge["primary_segment_id"],
                    "member_count": int(merge["member_count"]),
                    "merge_status": merge["status"],
                    "created_at": merge["created_at"],
                    "updated_at": merge["updated_at"],
                    "merge_members": [
                        {
                            "id": item["id"],
                            "title": item["title"],
                            "window_index": item["window_index"],
                            "global_start": item["global_start"],
                            "global_end": item["global_end"],
                            "absolute_start": item["absolute_start"],
                            "absolute_end": item["absolute_end"],
                            "role": item["merge_role"],
                            "member_order": item["merge_member_order"],
                        }
                        for item in members
                    ],
                }
            )

        combined = raw + merges

        def display_key(item: dict[str, Any]) -> tuple[float, int, int]:
            if item["record_kind"] == "merge":
                return float(item["global_start"]), 0, 0
            merge_id = item.get("active_merge_id")
            if merge_id:
                return (
                    merge_starts.get(str(merge_id), float(item["global_start"])),
                    1,
                    int(item.get("merge_member_order") or 0),
                )
            return float(item["global_start"]), 0, 1

        return sorted(combined, key=display_key)

    async def get_segment(self, job_id: str, segment_id: int) -> dict[str, Any] | None:
        async with self.connect() as db:
            row = await (
                await db.execute(
                    "SELECT s.*, w.window_index, mm.merge_id AS active_merge_id "
                    "FROM segments s JOIN windows w ON w.id=s.window_id "
                    "LEFT JOIN segment_merge_members mm "
                    "ON mm.segment_id=s.id AND mm.active=1 "
                    "WHERE s.id=? AND s.job_id=?",
                    (segment_id, job_id),
                )
            ).fetchone()
        return self._row(row)

    async def update_segment(
        self, job_id: str, segment_id: int, **fields: Any
    ) -> dict[str, Any] | None:
        allowed = {"title", "content_type", "ignored"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"Unknown segment fields: {sorted(unknown)}")
        if not fields:
            return None
        assignments = ", ".join(f"{key}=?" for key in fields)
        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            membership = await (
                await db.execute(
                    "SELECT merge_id FROM segment_merge_members "
                    "WHERE segment_id=? AND active=1",
                    (segment_id,),
                )
            ).fetchone()
            if membership is not None:
                await db.rollback()
                raise ValueError("Cancel the manual merge before editing a member")
            cursor = await db.execute(
                f"UPDATE segments SET {assignments} WHERE id=? AND job_id=?",
                (*fields.values(), segment_id, job_id),
            )
            if cursor.rowcount == 0:
                await db.rollback()
                return None
            updated = await (
                await db.execute(
                    "SELECT s.*, w.window_index FROM segments s "
                    "JOIN windows w ON w.id=s.window_id WHERE s.id=?",
                    (segment_id,),
                )
            ).fetchone()
            await db.commit()
        return self._row(updated)

    async def _segment_merge_preview_state(
        self,
        db: aiosqlite.Connection,
        job_id: str,
        segment_ids: list[int],
        primary_segment_id: int,
    ) -> dict[str, Any]:
        unique_ids = list(dict.fromkeys(segment_ids))
        if len(unique_ids) < 2:
            raise ValueError("Select at least two segments")
        if primary_segment_id not in unique_ids:
            raise ValueError("The primary segment must be in the selection")
        placeholders = ", ".join("?" for _ in unique_ids)
        rows = await (
            await db.execute(
                f"""
                SELECT s.*, w.window_index
                FROM segments s JOIN windows w ON w.id=s.window_id
                WHERE s.job_id=? AND s.id IN ({placeholders})
                ORDER BY s.global_start, s.global_end, s.id
                """,
                (job_id, *unique_ids),
            )
        ).fetchall()
        segments = [dict(row) for row in rows]
        if len(segments) != len(unique_ids):
            raise ValueError("One or more selected segments no longer exist")
        if any(not int(row["accepted"]) or int(row["ignored"]) for row in segments):
            raise ValueError("Only final adopted, non-ignored segments can be merged")
        membership = await (
            await db.execute(
                f"SELECT segment_id FROM segment_merge_members "
                f"WHERE active=1 AND segment_id IN ({placeholders})",
                unique_ids,
            )
        ).fetchone()
        if membership is not None:
            raise ValueError("A selected segment already belongs to a manual merge")

        eligible_rows = await (
            await db.execute(
                """
                SELECT s.id
                FROM segments s
                WHERE s.job_id=? AND s.accepted=1 AND s.ignored=0
                  AND NOT EXISTS(
                      SELECT 1 FROM segment_merge_members mm
                      WHERE mm.segment_id=s.id AND mm.active=1
                  )
                ORDER BY s.global_start, s.global_end, s.id
                """,
                (job_id,),
            )
        ).fetchall()
        positions = {
            int(row["id"]): index for index, row in enumerate(eligible_rows)
        }
        selected_positions = sorted(positions[int(row["id"])] for row in segments)
        if selected_positions != list(
            range(selected_positions[0], selected_positions[0] + len(segments))
        ):
            raise ValueError("Only consecutive final adopted segments can be merged")

        start = min(float(row["global_start"]) for row in segments)
        end = max(float(row["global_end"]) for row in segments)
        overlapping_merge = await (
            await db.execute(
                "SELECT id FROM segment_merges WHERE job_id=? AND status='active' "
                "AND global_start<? AND global_end>? LIMIT 1",
                (job_id, end, start),
            )
        ).fetchone()
        if overlapping_merge is not None:
            raise ValueError("The selected range crosses an existing manual merge")

        gap_seconds = 0.0
        overlap_seconds = 0.0
        for previous, current in zip(segments, segments[1:]):
            delta = float(current["global_start"]) - float(previous["global_end"])
            if delta > 0:
                gap_seconds += delta
            elif delta < 0:
                overlap_seconds += -delta
        primary = next(
            row for row in segments if int(row["id"]) == primary_segment_id
        )
        token_payload = {
            "job_id": job_id,
            "primary_segment_id": primary_segment_id,
            "segments": segments,
        }
        token = hashlib.sha256(
            json.dumps(
                token_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return {
            "segments": segments,
            "primary": primary,
            "global_start": start,
            "global_end": end,
            "gap_seconds": gap_seconds,
            "overlap_seconds": overlap_seconds,
            "preview_token": token,
        }

    async def preview_segment_merge(
        self, job_id: str, segment_ids: list[int], primary_segment_id: int
    ) -> dict[str, Any]:
        async with self.connect() as db:
            return await self._segment_merge_preview_state(
                db, job_id, segment_ids, primary_segment_id
            )

    async def create_segment_merge(
        self,
        job_id: str,
        segment_ids: list[int],
        primary_segment_id: int,
        expected_token: str,
    ) -> dict[str, Any]:
        now = utc_now()
        merge_id = uuid.uuid4().hex
        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            state = await self._segment_merge_preview_state(
                db, job_id, segment_ids, primary_segment_id
            )
            if state["preview_token"] != expected_token:
                await db.rollback()
                raise ValueError("The selected segments changed after preview")
            primary = state["primary"]
            segments = state["segments"]
            await db.execute(
                """
                INSERT INTO segment_merges(
                    id, job_id, primary_segment_id, status, title, content_type,
                    news_event_type, topic, keywords_json, summary,
                    global_start, global_end, ignored, member_count,
                    created_at, updated_at
                ) VALUES(?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
                """,
                (
                    merge_id,
                    job_id,
                    primary_segment_id,
                    primary["title"],
                    primary["content_type"],
                    primary["news_event_type"],
                    primary["topic"],
                    primary["keywords_json"],
                    primary["summary"],
                    state["global_start"],
                    state["global_end"],
                    len(segments),
                    now,
                    now,
                ),
            )
            for index, segment in enumerate(segments):
                await db.execute(
                    """
                    INSERT INTO segment_merge_members(
                        merge_id, segment_id, member_order, role, snapshot_json, active
                    ) VALUES(?, ?, ?, ?, ?, 1)
                    """,
                    (
                        merge_id,
                        segment["id"],
                        index,
                        "primary" if int(segment["id"]) == primary_segment_id else "member",
                        json.dumps(segment, ensure_ascii=False, sort_keys=True),
                    ),
                )
            await db.execute(
                "UPDATE jobs SET reviewed=0, updated_at=? WHERE id=?",
                (now, job_id),
            )
            row = await (
                await db.execute("SELECT * FROM segment_merges WHERE id=?", (merge_id,))
            ).fetchone()
            await db.commit()
        return dict(row)

    async def update_segment_merge(
        self, job_id: str, merge_id: str, **fields: Any
    ) -> dict[str, Any] | None:
        unknown = set(fields) - self.MERGE_FIELDS
        if unknown:
            raise ValueError(f"Unknown merge fields: {sorted(unknown)}")
        if not fields:
            return None
        now = utc_now()
        fields["updated_at"] = now
        assignments = ", ".join(f"{key}=?" for key in fields)
        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                f"UPDATE segment_merges SET {assignments} "
                "WHERE id=? AND job_id=? AND status='active'",
                (*fields.values(), merge_id, job_id),
            )
            if cursor.rowcount == 0:
                await db.rollback()
                return None
            await db.execute(
                "UPDATE jobs SET reviewed=0, updated_at=? WHERE id=?",
                (now, job_id),
            )
            row = await (
                await db.execute("SELECT * FROM segment_merges WHERE id=?", (merge_id,))
            ).fetchone()
            await db.commit()
        return dict(row)

    async def cancel_segment_merge(self, job_id: str, merge_id: str) -> bool:
        now = utc_now()
        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "UPDATE segment_merges SET status='cancelled', cancelled_at=?, "
                "updated_at=?, cancellation_reason='cancelled manually' "
                "WHERE id=? AND job_id=? AND status='active'",
                (now, now, merge_id, job_id),
            )
            if cursor.rowcount == 0:
                await db.rollback()
                return False
            await db.execute(
                "UPDATE segment_merge_members SET active=0 WHERE merge_id=?",
                (merge_id,),
            )
            await db.execute(
                "UPDATE jobs SET reviewed=0, updated_at=? WHERE id=?",
                (now, job_id),
            )
            await db.commit()
        return True

    async def get_job_merges(
        self, job_id: str, include_inactive: bool = True
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM segment_merges WHERE job_id=?"
        params: list[Any] = [job_id]
        if not include_inactive:
            query += " AND status='active'"
        query += " ORDER BY created_at, id"
        async with self.connect() as db:
            merges = await (await db.execute(query, params)).fetchall()
            result = []
            for merge_row in merges:
                merge = dict(merge_row)
                members = await (
                    await db.execute(
                        "SELECT * FROM segment_merge_members WHERE merge_id=? "
                        "ORDER BY member_order",
                        (merge["id"],),
                    )
                ).fetchall()
                merge["members"] = [dict(row) for row in members]
                result.append(merge)
        return result

    async def get_attempts_for_job(self, job_id: str) -> list[dict[str, Any]]:
        async with self.connect() as db:
            rows = await (
                await db.execute(
                    """
                    SELECT a.*, w.window_index, w.requested_start,
                           j.program_start_time AS job_program_start_time
                    FROM attempts a
                    JOIN windows w ON w.id=a.window_id
                    JOIN jobs j ON j.id=w.job_id
                    WHERE w.job_id=? ORDER BY w.window_index, a.attempt_no
                    """,
                    (job_id,),
                )
            ).fetchall()
        result = []
        for stored in rows:
            row = dict(stored)
            row["program_start_time"] = absolute_time(
                row.pop("job_program_start_time"), float(row["requested_start"])
            )
            result.append(row)
        return result

    async def ping(self) -> bool:
        try:
            async with self.connect() as db:
                row = await (await db.execute("SELECT 1")).fetchone()
            return bool(row and row[0] == 1)
        except Exception:
            return False
