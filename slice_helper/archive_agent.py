from __future__ import annotations

import argparse
import configparser
import contextlib
import hashlib
import json
import logging
import os
import re
import secrets
import shlex
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence

try:
    import fcntl
except ImportError:  # pragma: no cover - the deployed agent runs on Linux
    fcntl = None  # type: ignore[assignment]


logger = logging.getLogger("islice-archiver")
ARCHIVER_AGENT_VERSION = "3.2"
TASK_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
SOURCE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
RESET_REQUEST_PATTERN = re.compile(r"^[0-9a-f]{32}$")
RESET_NONCE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{20,128}$")
RESET_PROOF_PATTERN = re.compile(r"^[A-Za-z0-9_-]{20,128}$")
MEDIA_DIRS = ("segments", "covers")
DELETE_DIRS = ("video", "temp", "output")


class ArchiveError(RuntimeError):
    pass


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_time(value: datetime | None = None) -> str:
    return (value or utc_now()).isoformat()


def parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def require_task_id(value: str) -> str:
    if not TASK_ID_PATTERN.fullmatch(value):
        raise ArchiveError(f"Unsafe task ID: {value!r}")
    return value


def require_source_id(value: str) -> str:
    if not SOURCE_ID_PATTERN.fullmatch(value):
        raise ArchiveError(f"Unsafe source ID: {value!r}")
    return value


def is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


@dataclass(frozen=True, slots=True)
class ArchiveConfig:
    islice_database: Path
    storage_root: Path
    state_database: Path
    manifest_root: Path
    lock_path: Path
    remote_host: str
    remote_user: str
    remote_root: str
    remote_http_base: str
    ssh_key: Path
    known_hosts: Path
    source_id: str = "default"
    source_name: str = ""
    islice_base_url: str = ""
    slice_helper_base_url: str = ""
    delete_delay_hours: float = 24.0
    retry_delay_minutes: float = 30.0
    max_tasks_per_run: int = 4
    command_timeout_seconds: float = 21600.0
    http_timeout_seconds: float = 30.0
    reset_backup_root: Path = Path("/var/lib/islice-archiver/reset-backups")

    @classmethod
    def from_file(cls, path: Path) -> "ArchiveConfig":
        parser = configparser.ConfigParser()
        if not parser.read(path, encoding="utf-8"):
            raise ArchiveError(f"Configuration file does not exist: {path}")
        section = parser["archiver"]

        def required(name: str) -> str:
            value = section.get(name, "").strip()
            if not value:
                raise ArchiveError(f"Missing configuration value: {name}")
            return value

        config = cls(
            islice_database=Path(required("islice_database")),
            storage_root=Path(required("storage_root")),
            state_database=Path(required("state_database")),
            manifest_root=Path(required("manifest_root")),
            lock_path=Path(required("lock_path")),
            remote_host=required("remote_host"),
            remote_user=required("remote_user"),
            remote_root=required("remote_root").rstrip("/"),
            remote_http_base=required("remote_http_base").rstrip("/"),
            ssh_key=Path(required("ssh_key")),
            known_hosts=Path(required("known_hosts")),
            source_id=section.get("source_id", "default").strip() or "default",
            source_name=section.get("source_name", "").strip(),
            islice_base_url=section.get("islice_base_url", "").strip().rstrip("/"),
            slice_helper_base_url=section.get("slice_helper_base_url", "").strip().rstrip("/"),
            delete_delay_hours=section.getfloat("delete_delay_hours", 24.0),
            retry_delay_minutes=section.getfloat("retry_delay_minutes", 30.0),
            max_tasks_per_run=section.getint("max_tasks_per_run", 4),
            command_timeout_seconds=section.getfloat("command_timeout_seconds", 21600.0),
            http_timeout_seconds=section.getfloat("http_timeout_seconds", 30.0),
            reset_backup_root=Path(
                section.get(
                    "reset_backup_root",
                    str(Path(required("state_database")).parent / "reset-backups"),
                )
            ),
        )
        if config.delete_delay_hours < 0:
            raise ArchiveError("delete_delay_hours must not be negative")
        if config.max_tasks_per_run < 1:
            raise ArchiveError("max_tasks_per_run must be positive")
        if not config.remote_root.startswith("/"):
            raise ArchiveError("remote_root must be absolute")
        if not config.remote_http_base.startswith(("http://", "https://")):
            raise ArchiveError("remote_http_base must use HTTP or HTTPS")
        require_source_id(config.source_id)
        if not config.reset_backup_root.is_absolute():
            raise ArchiveError("reset_backup_root must be absolute")
        for name, value in (
            ("islice_base_url", config.islice_base_url),
            ("slice_helper_base_url", config.slice_helper_base_url),
        ):
            if value and not value.startswith(("http://", "https://")):
                raise ArchiveError(f"{name} must use HTTP or HTTPS")
        return config


def require_reset_request_id(value: str) -> str:
    if not RESET_REQUEST_PATTERN.fullmatch(value):
        raise ArchiveError("Reset request ID must be 32 lowercase hexadecimal characters")
    return value


def require_reset_nonce(value: str) -> str:
    if not RESET_NONCE_PATTERN.fullmatch(value):
        raise ArchiveError("Reset nonce is invalid")
    return value


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def backup_sqlite(source: Path, destination: Path) -> None:
    source = source.resolve()
    destination = destination.resolve()
    if not source.is_file():
        raise ArchiveError(f"SQLite database does not exist: {source}")
    if source == destination:
        raise ArchiveError("SQLite backup destination must differ from its source")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".partial")
    temporary.unlink(missing_ok=True)
    source_connection: sqlite3.Connection | None = None
    destination_connection: sqlite3.Connection | None = None
    try:
        source_connection = sqlite3.connect(source, timeout=30.0)
        destination_connection = sqlite3.connect(temporary)
        source_connection.backup(destination_connection)
        row = destination_connection.execute("PRAGMA integrity_check").fetchone()
        if not row or row[0] != "ok":
            raise ArchiveError(f"SQLite backup integrity check failed: {row}")
        destination_connection.close()
        destination_connection = None
        source_connection.close()
        source_connection = None
        os.replace(temporary, destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    finally:
        if destination_connection is not None:
            destination_connection.close()
        if source_connection is not None:
            source_connection.close()


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".partial")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def reset_request_directory(config: ArchiveConfig, request_id: str) -> Path:
    request_id = require_reset_request_id(request_id)
    root = config.reset_backup_root.resolve()
    directory = (root / request_id).resolve()
    try:
        directory.relative_to(root)
    except ValueError as exc:  # pragma: no cover - guarded by the ID pattern
        raise ArchiveError("Reset request path escaped reset_backup_root") from exc
    return directory


@dataclass(frozen=True, slots=True)
class ManifestFile:
    path: str
    size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class TaskManifest:
    task_id: str
    files: tuple[ManifestFile, ...]
    total_bytes: int
    deletion_eligible: bool
    warnings: tuple[str, ...]
    payload: dict[str, Any]
    digest: str

    @property
    def media_files(self) -> tuple[ManifestFile, ...]:
        return tuple(
            item for item in self.files if item.path.startswith(("segments/", "covers/"))
        )

    @property
    def media_paths(self) -> frozenset[str]:
        return frozenset(item.path for item in self.media_files)


@dataclass(frozen=True, slots=True)
class RemoteSyncResult:
    published: bool
    remote_path: str
    previous_digest: str | None = None
    previous_remote_path: str | None = None
    hold_reason: str = ""


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _expected_media_paths(task_id: str, segments_path: Path) -> tuple[set[str], list[str]]:
    try:
        payload = json.loads(segments_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ArchiveError(f"Invalid segments.json: {exc}") from exc
    segments = payload.get("segments") if isinstance(payload, dict) else None
    if not isinstance(segments, list) or not segments:
        raise ArchiveError("segments.json does not contain any segments")

    expected: set[str] = set()
    warnings: list[str] = []
    for index, segment in enumerate(segments, start=1):
        if not isinstance(segment, dict):
            warnings.append(f"Segment {index} is not an object")
            continue
        for field, directory in (("segmentUrl", "segments"), ("coverImgUrl", "covers")):
            raw_url = str(segment.get(field) or "")
            if not raw_url:
                warnings.append(f"Segment {index} has an empty {field}")
                continue
            parts = urllib.parse.urlsplit(raw_url).path.strip("/").split("/")
            try:
                download_index = parts.index("download")
                url_task, url_directory, filename = parts[download_index + 1 : download_index + 4]
            except (ValueError, IndexError) as exc:
                warnings.append(f"Segment {index} has an invalid {field}: {raw_url}")
                continue
            if url_task != task_id or url_directory != directory or not filename:
                warnings.append(f"Segment {index} has a mismatched {field}: {raw_url}")
                continue
            expected.add(f"{directory}/{filename}")
    return expected, warnings


def build_manifest(task: dict[str, Any], output_dir: Path) -> TaskManifest:
    task_id = require_task_id(str(task["task_id"]))
    if not output_dir.is_dir():
        raise ArchiveError(f"Output directory does not exist: {output_dir}")
    if output_dir.is_symlink():
        raise ArchiveError("Output directory must not be a symlink")

    files: list[ManifestFile] = []
    for path in sorted(output_dir.rglob("*")):
        if path.is_symlink():
            raise ArchiveError(f"Archive input contains a symlink: {path}")
        if not path.is_file():
            continue
        relative = path.relative_to(output_dir).as_posix()
        if relative.startswith(".rsync-partial/") or relative.endswith((".partial", ".tmp")):
            raise ArchiveError(f"Archive input contains an incomplete file: {relative}")
        stat = path.stat()
        files.append(ManifestFile(relative, stat.st_size, file_sha256(path)))
    if not files:
        raise ArchiveError("Output directory is empty")

    by_path = {item.path for item in files}
    expected, warnings = _expected_media_paths(task_id, output_dir / "segments.json")
    missing = sorted(expected - by_path)
    warnings.extend(f"Referenced media file is missing: {path}" for path in missing)
    if not any(item.path.startswith("segments/") for item in files):
        warnings.append("No segment video files were found")

    payload: dict[str, Any] = {
        "schemaVersion": 1,
        "taskId": task_id,
        "generatedAt": iso_time(),
        "task": {
            key: task.get(key)
            for key in (
                "video_path",
                "template_id",
                "channel_name",
                "program_start_time",
                "status",
                "start_time",
                "end_time",
                "language",
            )
        },
        "deletionEligible": not warnings,
        "warnings": warnings,
        "fileCount": len(files),
        "totalBytes": sum(item.size for item in files),
        "files": [
            {"path": item.path, "size": item.size, "sha256": item.sha256}
            for item in files
        ],
    }
    digest_payload = {key: value for key, value in payload.items() if key != "generatedAt"}
    canonical = json.dumps(
        digest_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    payload["manifestDigest"] = digest
    return TaskManifest(
        task_id=task_id,
        files=tuple(files),
        total_bytes=int(payload["totalBytes"]),
        deletion_eligible=bool(payload["deletionEligible"]),
        warnings=tuple(warnings),
        payload=payload,
        digest=digest,
    )


def write_manifest_files(manifest: TaskManifest, root: Path) -> tuple[Path, Path]:
    directory = root / manifest.task_id
    directory.mkdir(parents=True, exist_ok=True)
    json_path = directory / "manifest.json"
    checksum_path = directory / "manifest.sha256"
    temporary_json = json_path.with_suffix(".json.tmp")
    temporary_checksums = checksum_path.with_suffix(".sha256.tmp")
    temporary_json.write_text(
        json.dumps(manifest.payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary_checksums.write_text(
        "".join(f"{item.sha256}  {item.path}\n" for item in manifest.files),
        encoding="utf-8",
    )
    os.replace(temporary_json, json_path)
    os.replace(temporary_checksums, checksum_path)
    return json_path, checksum_path


class StateStore:
    def __init__(self, path: Path, source_id: str = "default"):
        self.path = path
        self.source_id = require_source_id(source_id)

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS archives (
                    task_id TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    local_task_path TEXT NOT NULL,
                    local_output_path TEXT NOT NULL,
                    remote_path TEXT NOT NULL,
                    manifest_digest TEXT NOT NULL DEFAULT '',
                    file_count INTEGER NOT NULL DEFAULT 0,
                    total_bytes INTEGER NOT NULL DEFAULT 0,
                    deletion_eligible INTEGER NOT NULL DEFAULT 0,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    error_message TEXT NOT NULL DEFAULT '',
                    warnings_json TEXT NOT NULL DEFAULT '[]',
                    discovered_at TEXT NOT NULL,
                    archived_at TEXT,
                    delete_after TEXT,
                    deleted_at TEXT,
                    next_attempt_at TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS archive_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    event TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_archives_state
                    ON archives(state, next_attempt_at, delete_after);
                CREATE TABLE IF NOT EXISTS archive_revisions (
                    source_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    manifest_digest TEXT NOT NULL,
                    state TEXT NOT NULL,
                    remote_path TEXT NOT NULL,
                    file_count INTEGER NOT NULL DEFAULT 0,
                    total_bytes INTEGER NOT NULL DEFAULT 0,
                    warnings_json TEXT NOT NULL DEFAULT '[]',
                    published_at TEXT,
                    superseded_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(source_id, task_id, manifest_digest)
                );
                CREATE INDEX IF NOT EXISTS idx_archive_revisions_task
                    ON archive_revisions(source_id, task_id, created_at);
                """
            )
            columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(archives)")
            }
            migrations = {
                "source_id": "TEXT NOT NULL DEFAULT ''",
                "source_modified_at": "TEXT NOT NULL DEFAULT ''",
                "published_digest": "TEXT NOT NULL DEFAULT ''",
                "revision_count": "INTEGER NOT NULL DEFAULT 0",
            }
            for name, definition in migrations.items():
                if name not in columns:
                    connection.execute(
                        f"ALTER TABLE archives ADD COLUMN {name} {definition}"
                    )
            connection.execute(
                "UPDATE archives SET source_id=? WHERE source_id=''", (self.source_id,)
            )
            connection.execute(
                "UPDATE archives SET state='pending', error_message='Recovered after interruption' "
                "WHERE state IN ('syncing', 'verifying')"
            )
            now = iso_time()
            connection.execute(
                """
                INSERT OR IGNORE INTO archive_revisions (
                    source_id, task_id, manifest_digest, state, remote_path,
                    file_count, total_bytes, warnings_json, published_at,
                    created_at, updated_at
                )
                SELECT source_id, task_id, manifest_digest,
                       CASE WHEN state='archived_hold' THEN 'published_hold' ELSE 'published' END,
                       remote_path, file_count, total_bytes, warnings_json,
                       archived_at, COALESCE(archived_at, discovered_at), ?
                FROM archives WHERE manifest_digest<>''
                """,
                (now,),
            )
            connection.execute(
                """
                UPDATE archives SET revision_count=(
                    SELECT COUNT(*) FROM archive_revisions r
                    WHERE r.source_id=archives.source_id AND r.task_id=archives.task_id
                ), published_digest=CASE
                    WHEN published_digest='' THEN manifest_digest ELSE published_digest END
                """
            )

    def discover(
        self,
        task_id: str,
        task_path: Path,
        output_path: Path,
        remote_path: str,
        source_modified_at: str,
    ) -> None:
        now = iso_time()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO archives (
                    task_id, state, local_task_path, local_output_path, remote_path,
                    discovered_at, updated_at, source_id, source_modified_at
                ) VALUES (?, 'pending', ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    str(task_path),
                    str(output_path),
                    remote_path,
                    now,
                    now,
                    self.source_id,
                    source_modified_at,
                ),
            )
            if cursor.rowcount:
                self._event(connection, task_id, "discovered", str(output_path))
                return
            row = connection.execute(
                "SELECT state,source_modified_at FROM archives WHERE task_id=?", (task_id,)
            ).fetchone()
            if (
                row
                and output_path.is_dir()
                and str(row["source_modified_at"] or "") != source_modified_at
            ):
                connection.execute(
                    """
                    UPDATE archives
                    SET state='pending', local_task_path=?, local_output_path=?, remote_path=?,
                        source_modified_at=?, error_message='', next_attempt_at=NULL,
                        updated_at=? WHERE task_id=?
                    """,
                    (
                        str(task_path),
                        str(output_path),
                        remote_path,
                        source_modified_at,
                        now,
                        task_id,
                    ),
                )
                self._event(
                    connection,
                    task_id,
                    "source_revision_discovered",
                    source_modified_at,
                )

    def candidates(self, limit: int) -> list[dict[str, Any]]:
        now = iso_time()
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM archives
                WHERE state='pending'
                   OR (state='failed' AND (next_attempt_at IS NULL OR next_attempt_at <= ?))
                   OR (state='archived_unpublished' AND next_attempt_at <= ?)
                ORDER BY discovered_at
                LIMIT ?
                """,
                (now, now, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def due_for_deletion(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM archives WHERE state='delete_pending' AND delete_after <= ? "
                "ORDER BY delete_after",
                (iso_time(),),
            ).fetchall()
        return [dict(row) for row in rows]

    def get(self, task_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM archives WHERE task_id=?", (task_id,)
            ).fetchone()
        return dict(row) if row else None

    def list(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM archives ORDER BY discovered_at DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def revisions(self, task_id: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM archive_revisions WHERE source_id=?"
        params: list[Any] = [self.source_id]
        if task_id is not None:
            query += " AND task_id=?"
            params.append(task_id)
        query += " ORDER BY created_at DESC"
        with self.connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def record_revision(
        self,
        task_id: str,
        manifest: TaskManifest,
        result: RemoteSyncResult,
        state: str,
    ) -> None:
        now = iso_time()
        with self.connect() as connection:
            if result.previous_digest:
                connection.execute(
                    """
                    UPDATE archive_revisions SET state='superseded', superseded_at=?,
                        remote_path=COALESCE(?,remote_path), updated_at=?
                    WHERE source_id=? AND task_id=? AND manifest_digest=?
                    """,
                    (
                        now,
                        result.previous_remote_path,
                        now,
                        self.source_id,
                        task_id,
                        result.previous_digest,
                    ),
                )
            connection.execute(
                """
                INSERT INTO archive_revisions (
                    source_id,task_id,manifest_digest,state,remote_path,file_count,
                    total_bytes,warnings_json,published_at,created_at,updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(source_id,task_id,manifest_digest) DO UPDATE SET
                    state=excluded.state, remote_path=excluded.remote_path,
                    file_count=excluded.file_count, total_bytes=excluded.total_bytes,
                    warnings_json=excluded.warnings_json,
                    published_at=COALESCE(excluded.published_at,archive_revisions.published_at),
                    updated_at=excluded.updated_at
                """,
                (
                    self.source_id,
                    task_id,
                    manifest.digest,
                    state,
                    result.remote_path,
                    len(manifest.files),
                    manifest.total_bytes,
                    json.dumps(manifest.warnings, ensure_ascii=False),
                    now if result.published else None,
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE archives SET revision_count=(
                    SELECT COUNT(*) FROM archive_revisions
                    WHERE source_id=? AND task_id=?
                ), published_digest=CASE WHEN ? THEN ? ELSE published_digest END
                WHERE task_id=?
                """,
                (
                    self.source_id,
                    task_id,
                    int(result.published),
                    manifest.digest,
                    task_id,
                ),
            )

    def catalog(self, config: ArchiveConfig) -> dict[str, Any]:
        tasks = self.list()
        revisions = self.revisions()

        def public_archive_url(remote_path: Any) -> str:
            root = config.remote_root.rstrip("/")
            path = str(remote_path or "").rstrip("/")
            if path == root:
                return config.remote_http_base
            if not path.startswith(root + "/"):
                return ""
            relative = path[len(root) + 1 :]
            encoded = "/".join(
                urllib.parse.quote(part, safe="") for part in relative.split("/")
            )
            return f"{config.remote_http_base}/{encoded}"

        by_task: dict[str, list[dict[str, Any]]] = {}
        for revision in revisions:
            revision["archive_url"] = public_archive_url(revision.get("remote_path"))
            by_task.setdefault(str(revision["task_id"]), []).append(revision)
        counts: dict[str, int] = {}
        total_bytes = 0
        for row in tasks:
            state = str(row["state"])
            counts[state] = counts.get(state, 0) + 1
            total_bytes += int(row.get("total_bytes") or 0)
            row["archive_url"] = public_archive_url(row.get("remote_path"))
            row["revisions"] = by_task.get(str(row["task_id"]), [])
        return {
            "schemaVersion": 3,
            "generatedAt": iso_time(),
            "source": {
                "id": config.source_id,
                "name": config.source_name or config.source_id,
                "isliceBaseUrl": config.islice_base_url,
                "archiveHttpBase": config.remote_http_base,
            },
            "summary": {
                "taskCount": len(tasks),
                "totalBytes": total_bytes,
                "states": counts,
            },
            "tasks": tasks,
        }

    def rebase_current_paths(self, remote_root: str) -> None:
        with self.connect() as connection:
            rows = connection.execute("SELECT task_id FROM archives").fetchall()
            for row in rows:
                task_id = require_task_id(str(row["task_id"]))
                current = f"{remote_root}/tasks/{task_id}"
                connection.execute(
                    "UPDATE archives SET remote_path=? WHERE task_id=?",
                    (current, task_id),
                )
                connection.execute(
                    """
                    UPDATE archive_revisions SET remote_path=?
                    WHERE source_id=? AND task_id=? AND state IN ('published','published_hold')
                    """,
                    (current, self.source_id, task_id),
                )

    def transition(self, task_id: str, state: str, event: str, **fields: Any) -> None:
        fields = {**fields, "state": state, "updated_at": iso_time()}
        assignments = ", ".join(f"{key}=?" for key in fields)
        values = list(fields.values()) + [task_id]
        with self.connect() as connection:
            connection.execute(f"UPDATE archives SET {assignments} WHERE task_id=?", values)
            self._event(connection, task_id, event, str(fields.get("error_message") or ""))

    def fail(self, task_id: str, error: str, retry_delay: timedelta) -> None:
        row = self.get(task_id)
        attempts = int(row["attempt_count"] if row else 0) + 1
        self.transition(
            task_id,
            "failed",
            "failed",
            attempt_count=attempts,
            error_message=error[:4000],
            next_attempt_at=iso_time(utc_now() + retry_delay),
        )

    def retry(self, task_id: str) -> None:
        self.transition(
            task_id,
            "pending",
            "manual_retry",
            error_message="",
            next_attempt_at=None,
        )

    @staticmethod
    def _event(connection: sqlite3.Connection, task_id: str, event: str, detail: str) -> None:
        connection.execute(
            "INSERT INTO archive_events(task_id,event,detail,created_at) VALUES(?,?,?,?)",
            (task_id, event, detail[:4000], iso_time()),
        )


class ISliceTasks:
    def __init__(self, database: Path, storage_root: Path):
        self.database = database
        self.storage_root = storage_root.resolve()

    def completed(self) -> Iterator[tuple[dict[str, Any], Path, Path]]:
        uri = f"file:{self.database.as_posix()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        connection.row_factory = sqlite3.Row
        try:
            for row in connection.execute("SELECT * FROM tasks WHERE status='completed'"):
                task = dict(row)
                task_id = require_task_id(str(task["task_id"]))
                task_path = (self.storage_root / task_id).resolve()
                if not is_within(task_path, self.storage_root):
                    raise ArchiveError(f"Task path escapes storage root: {task_path}")
                raw_output = str(task.get("output_dir") or "").strip()
                output_path = Path(raw_output) if raw_output else task_path / "output"
                if not output_path.is_absolute():
                    output_path = task_path / output_path
                output_path = output_path.resolve()
                if not is_within(output_path, task_path):
                    logger.warning("Task %s uses an external output directory; deletion will be held", task_id)
                yield task, task_path, output_path
        finally:
            connection.close()

    def get(self, task_id: str) -> dict[str, Any]:
        uri = f"file:{self.database.as_posix()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        connection.row_factory = sqlite3.Row
        try:
            row = connection.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        finally:
            connection.close()
        if not row:
            raise ArchiveError(f"iSlice task no longer exists: {task_id}")
        return dict(row)


class RemoteArchive:
    def __init__(self, config: ArchiveConfig):
        self.config = config

    @property
    def ssh_options(self) -> list[str]:
        return [
            "-i",
            str(self.config.ssh_key),
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            f"UserKnownHostsFile={self.config.known_hosts}",
            "-o",
            "ConnectTimeout=15",
        ]

    def _run(self, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        try:
            result = subprocess.run(
                list(args),
                check=False,
                capture_output=True,
                text=True,
                timeout=self.config.command_timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ArchiveError(f"Command failed to run: {args[0]}: {exc}") from exc
        if result.returncode:
            message = (result.stderr or result.stdout).strip()
            raise ArchiveError(f"{args[0]} exited {result.returncode}: {message[-2000:]}")
        return result

    def ssh(self, command: str) -> str:
        result = self._run(
            [
                "ssh",
                *self.ssh_options,
                f"{self.config.remote_user}@{self.config.remote_host}",
                command,
            ]
        )
        return result.stdout

    def ensure_layout(self) -> None:
        root = self.config.remote_root
        download = f"{root}/download"
        self.ssh(
            "set -eu; "
            f"mkdir -p {shlex.quote(f'{root}/incoming')} "
            f"{shlex.quote(f'{root}/tasks')} {shlex.quote(f'{root}/history')}; "
            f"if test ! -e {shlex.quote(download)}; then "
            f"ln -s tasks {shlex.quote(download)}; fi; "
            f"test \"$(readlink {shlex.quote(download)})\" = tasks"
        )

    def sync(
        self,
        manifest: TaskManifest,
        output_dir: Path,
        manifest_json: Path,
        manifest_checksums: Path,
        *,
        publish: bool = True,
        hold_reason: str = "",
    ) -> RemoteSyncResult:
        self.ensure_layout()
        task_id = require_task_id(manifest.task_id)
        staging = f"{self.config.remote_root}/incoming/{task_id}.{manifest.digest}.partial"
        final = f"{self.config.remote_root}/tasks/{task_id}"
        remote_manifest = f"{final}/manifest.json"
        existing = self.ssh(
            f"if test -f {shlex.quote(remote_manifest)}; then cat {shlex.quote(remote_manifest)}; fi"
        ).strip()
        if existing:
            try:
                existing_digest = json.loads(existing).get("manifestDigest")
            except json.JSONDecodeError as exc:
                raise ArchiveError(f"Remote manifest is invalid for {task_id}") from exc
            if existing_digest == manifest.digest:
                self.verify(manifest)
                return RemoteSyncResult(True, final)
        else:
            existing_digest = None

        stored_revision = f"{self.config.remote_root}/history/{task_id}/{manifest.digest}"
        stored_manifest = f"{stored_revision}/manifest.json"
        stored = self.ssh(
            f"if test -f {shlex.quote(stored_manifest)}; then cat {shlex.quote(stored_manifest)}; fi"
        ).strip()
        if stored:
            try:
                stored_digest = json.loads(stored).get("manifestDigest")
            except json.JSONDecodeError as exc:
                raise ArchiveError(f"Stored revision manifest is invalid for {task_id}") from exc
            if stored_digest != manifest.digest:
                raise ArchiveError(f"Stored revision conflict for {task_id}")
            self.verify(manifest, stored_revision)
            if not publish:
                return RemoteSyncResult(
                    False,
                    stored_revision,
                    existing_digest,
                    None,
                    hold_reason,
                )
            staging = stored_revision
        else:
            self.ssh(f"mkdir -p {shlex.quote(staging)}")
            remote = f"{self.config.remote_user}@{self.config.remote_host}:{staging}/"
            rsync_ssh = "ssh " + " ".join(shlex.quote(item) for item in self.ssh_options)
            common = [
                "rsync",
                "-a",
                "--partial",
                "--partial-dir=.rsync-partial",
                "--protect-args",
                "-e",
                rsync_ssh,
            ]
            self._run([*common, f"{output_dir}/", remote])
            self._run([*common, str(manifest_json), str(manifest_checksums), remote])
            self.ssh(
                f"set -eu; cd {shlex.quote(staging)}; sha256sum -c manifest.sha256 >/dev/null"
            )

        if not publish:
            history = stored_revision
            self.ssh(
                "set -eu; "
                f"mkdir -p {shlex.quote(f'{self.config.remote_root}/history/{task_id}')}; "
                f"mv {shlex.quote(staging)} {shlex.quote(history)}"
            )
            return RemoteSyncResult(False, history, existing_digest, None, hold_reason)

        history_base = f"{self.config.remote_root}/history/{task_id}"
        history = (
            f"{history_base}/{existing_digest}"
            if existing_digest
            else ""
        )
        duplicate_history = f"{history}.{uuid.uuid4().hex[:8]}" if history else ""
        previous_remote_path = None
        if existing_digest:
            history_exists = self.ssh(
                f"if test -e {shlex.quote(history)}; then echo yes; fi"
            ).strip()
            previous_remote_path = duplicate_history if history_exists else history
            self.ssh(
                "set -eu; "
                f"mkdir -p {shlex.quote(history_base)}; "
                f"old_target={shlex.quote(previous_remote_path)}; "
                f"mv {shlex.quote(final)} \"$old_target\"; "
                f"if ! mv {shlex.quote(staging)} {shlex.quote(final)}; then "
                f"  mv \"$old_target\" {shlex.quote(final)}; exit 1; "
                "fi"
            )
        else:
            self.ssh(
                f"set -eu; mkdir -p {shlex.quote(f'{self.config.remote_root}/tasks')}; "
                f"test ! -e {shlex.quote(final)}; mv {shlex.quote(staging)} {shlex.quote(final)}"
            )
        self.verify(manifest)
        return RemoteSyncResult(
            True, final, existing_digest, previous_remote_path
        )

    def verify(self, manifest: TaskManifest, remote_path: str | None = None) -> None:
        final = remote_path or f"{self.config.remote_root}/tasks/{require_task_id(manifest.task_id)}"
        self.ssh(
            f"set -eu; cd {shlex.quote(final)}; sha256sum -c manifest.sha256 >/dev/null"
        )
        if remote_path is None:
            for item in manifest.media_files:
                self._verify_http(manifest.task_id, item)

    def publish_catalog(self, catalog_path: Path) -> None:
        remote_partial = f"{self.config.remote_root}/catalog.json.partial"
        remote_final = f"{self.config.remote_root}/catalog.json"
        remote = f"{self.config.remote_user}@{self.config.remote_host}:{remote_partial}"
        rsync_ssh = "ssh " + " ".join(shlex.quote(item) for item in self.ssh_options)
        self.ensure_layout()
        self._run(["rsync", "-a", "--protect-args", "-e", rsync_ssh, str(catalog_path), remote])
        self.ssh(
            f"set -eu; mv {shlex.quote(remote_partial)} {shlex.quote(remote_final)}"
        )

    def _verify_http(self, task_id: str, item: ManifestFile) -> None:
        encoded_path = "/".join(urllib.parse.quote(part) for part in item.path.split("/"))
        url = f"{self.config.remote_http_base}/download/{urllib.parse.quote(task_id)}/{encoded_path}"
        head = urllib.request.Request(url, method="HEAD")
        try:
            with urllib.request.urlopen(head, timeout=self.config.http_timeout_seconds) as response:
                if response.status != 200:
                    raise ArchiveError(f"Archive HEAD returned {response.status}: {url}")
                if int(response.headers.get("Content-Length", "-1")) != item.size:
                    raise ArchiveError(f"Archive HTTP size differs for {item.path}")
            ranged = urllib.request.Request(url, headers={"Range": "bytes=0-0"})
            with urllib.request.urlopen(ranged, timeout=self.config.http_timeout_seconds) as response:
                if response.status != 206 or response.read(2) == b"":
                    raise ArchiveError(f"Archive Range validation failed: {url}")
        except (urllib.error.URLError, ValueError) as exc:
            raise ArchiveError(f"Archive HTTP validation failed for {item.path}: {exc}") from exc


class Archiver:
    def __init__(
        self,
        config: ArchiveConfig,
        state: StateStore | None = None,
        tasks: ISliceTasks | None = None,
        remote: RemoteArchive | None = None,
    ):
        self.config = config
        self.state = state or StateStore(config.state_database, config.source_id)
        self.tasks = tasks or ISliceTasks(config.islice_database, config.storage_root)
        self.remote = remote or RemoteArchive(config)

    @contextlib.contextmanager
    def lock(self) -> Iterator[None]:
        if fcntl is None:
            yield
            return
        self.config.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self.config.lock_path.open("w", encoding="ascii") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ArchiveError("Another archiver run is active") from exc
            yield

    def run_once(self) -> None:
        with self.lock():
            self.state.initialize()
            self.state.rebase_current_paths(self.config.remote_root)
            self.discover()
            self.delete_due()
            # The task database is the source of truth. Scan all candidates on
            # every five-minute cycle; storage itself is still copied per task
            # only when its manifest changed, so this is not a storage-wide copy.
            candidates = self.state.candidates(max(self.config.max_tasks_per_run, 100000))
            logger.info("Archive scan found %d task(s) ready for processing", len(candidates))
            for row in candidates:
                self.archive(row)
            self.publish_catalog()

    def run_forever(self, interval_seconds: float) -> None:
        if interval_seconds < 10:
            raise ArchiveError("run-forever interval must be at least 10 seconds")
        logger.info(
            "Archive agent %s running every %.0f seconds",
            ARCHIVER_AGENT_VERSION,
            interval_seconds,
        )
        while True:
            started = time.monotonic()
            try:
                self.run_once()
            except Exception:
                logger.exception("Archive cycle failed")
            delay = max(1.0, interval_seconds - (time.monotonic() - started))
            time.sleep(delay)

    def discover(self) -> None:
        for task, task_path, output_path in self.tasks.completed():
            task_id = require_task_id(str(task["task_id"]))
            self.state.discover(
                task_id,
                task_path,
                output_path,
                f"{self.config.remote_root}/tasks/{task_id}",
                str(task.get("modify_time") or ""),
            )

    def _publication_decision(self, manifest: TaskManifest) -> tuple[bool, str]:
        if not self.config.slice_helper_base_url:
            return True, ""
        url = (
            f"{self.config.slice_helper_base_url}/internal/archive-references/"
            f"{urllib.parse.quote(manifest.task_id)}"
        )
        if self.config.islice_base_url:
            url += "?" + urllib.parse.urlencode(
                {"isliceBaseUrl": self.config.islice_base_url}
            )
        try:
            with urllib.request.urlopen(url, timeout=self.config.http_timeout_seconds) as response:
                payload = json.load(response)
        except (urllib.error.URLError, ValueError, json.JSONDecodeError) as exc:
            raise ArchiveError(
                f"Cannot verify whether slice_helper accepted {manifest.task_id}: {exc}"
            ) from exc
        if not payload.get("found"):
            return True, ""
        expected = {str(item) for item in payload.get("mediaPaths") or []}
        if expected and expected.issubset(manifest.media_paths):
            return True, ""
        missing = sorted(expected - manifest.media_paths)
        return (
            False,
            "slice_helper still references another revision"
            + (f": {', '.join(missing[:5])}" if missing else ""),
        )

    def archive(self, row: dict[str, Any]) -> None:
        task_id = require_task_id(str(row["task_id"]))
        try:
            logger.info("Archiving completed task %s", task_id)
            task = self.tasks.get(task_id)
            if task.get("status") != "completed":
                raise ArchiveError(f"Task is no longer completed: {task.get('status')}")
            output_path = Path(row["local_output_path"])
            manifest = build_manifest(task, output_path)
            manifest_json, manifest_checksums = write_manifest_files(
                manifest, self.config.manifest_root
            )
            publish, hold_reason = self._publication_decision(manifest)
            self.state.transition(
                task_id,
                "syncing",
                "sync_started",
                manifest_digest=manifest.digest,
                file_count=len(manifest.files),
                total_bytes=manifest.total_bytes,
                deletion_eligible=int(manifest.deletion_eligible),
                warnings_json=json.dumps(manifest.warnings, ensure_ascii=False),
                source_modified_at=str(task.get("modify_time") or ""),
                error_message="",
            )
            result = self.remote.sync(
                manifest,
                output_path,
                manifest_json,
                manifest_checksums,
                publish=publish,
                hold_reason=hold_reason,
            )
            revision_state = "published" if result.published else "unpublished"
            self.state.record_revision(
                task_id, manifest, result, revision_state
            )
            self.state.transition(task_id, "verifying", "remote_verified")
            archived_at = utc_now()
            if not result.published:
                self.state.transition(
                    task_id,
                    "archived_unpublished",
                    "archive_completed_unpublished",
                    archived_at=iso_time(archived_at),
                    delete_after=None,
                    next_attempt_at=iso_time(
                        archived_at + timedelta(minutes=self.config.retry_delay_minutes)
                    ),
                    error_message=hold_reason,
                )
                logger.warning(
                    "Task %s revision was archived but not published: %s",
                    task_id,
                    hold_reason,
                )
                return
            if manifest.deletion_eligible and is_within(
                output_path, Path(row["local_task_path"])
            ):
                self.state.transition(
                    task_id,
                    "delete_pending",
                    "archive_completed",
                    archived_at=iso_time(archived_at),
                    delete_after=iso_time(
                        archived_at + timedelta(hours=self.config.delete_delay_hours)
                    ),
                    next_attempt_at=None,
                    error_message="",
                )
                logger.info(
                    "Task %s archived; local deletion is scheduled for %s",
                    task_id,
                    iso_time(archived_at + timedelta(hours=self.config.delete_delay_hours)),
                )
            else:
                self.state.transition(
                    task_id,
                    "archived_hold",
                    "archive_completed_with_hold",
                    archived_at=iso_time(archived_at),
                    delete_after=None,
                    next_attempt_at=None,
                    error_message="",
                )
                logger.warning(
                    "Task %s archived with local deletion held: %s",
                    task_id,
                    "; ".join(manifest.warnings) or "output directory is outside the task",
                )
        except Exception as exc:
            logger.exception("Task %s archive failed", task_id)
            self.state.fail(task_id, str(exc), timedelta(minutes=self.config.retry_delay_minutes))

    def delete_due(self) -> None:
        for row in self.state.due_for_deletion():
            task_id = require_task_id(str(row["task_id"]))
            try:
                logger.info("Revalidating task %s before delayed local deletion", task_id)
                task = self.tasks.get(task_id)
                output_path = Path(row["local_output_path"])
                current = build_manifest(task, output_path)
                if current.digest != row["manifest_digest"]:
                    raise ArchiveError("Local output changed after it was archived")
                self.remote.verify(current)
                self._delete_local(row, current)
                self.state.transition(
                    task_id,
                    "deleted",
                    "local_deleted",
                    deleted_at=iso_time(),
                    error_message="",
                )
                logger.info("Deleted archived local media for task %s", task_id)
            except Exception as exc:
                logger.exception("Task %s delayed deletion failed", task_id)
                self.state.fail(task_id, str(exc), timedelta(minutes=self.config.retry_delay_minutes))

    def publish_catalog(self) -> None:
        if not hasattr(self.remote, "publish_catalog"):
            return
        payload = self.state.catalog(self.config)
        path = self.config.manifest_root / "catalog.json"
        temporary = path.with_suffix(".json.tmp")
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
        self.remote.publish_catalog(path)

    def _delete_local(self, row: dict[str, Any], manifest: TaskManifest) -> None:
        task_path = Path(row["local_task_path"]).resolve()
        storage_root = self.config.storage_root.resolve()
        if task_path.parent != storage_root or task_path.name != manifest.task_id:
            raise ArchiveError(f"Refusing unsafe local deletion: {task_path}")
        marker = task_path / "archive.json"
        marker_temp = task_path / "archive.json.tmp"
        marker_temp.write_text(
            json.dumps(
                {
                    "taskId": manifest.task_id,
                    "manifestDigest": manifest.digest,
                    "remotePath": row["remote_path"],
                    "archivedAt": row["archived_at"],
                    "deletedAt": iso_time(),
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        os.replace(marker_temp, marker)
        for name in DELETE_DIRS:
            target = (task_path / name).resolve()
            if target.parent != task_path or target.name != name:
                raise ArchiveError(f"Refusing unsafe child deletion: {target}")
            if target.is_dir():
                shutil.rmtree(target)
            elif target.exists():
                target.unlink()


def load_reset_receipt(config: ArchiveConfig, request_id: str) -> tuple[Path, dict[str, Any]]:
    directory = reset_request_directory(config, request_id)
    receipt_path = directory / "receipt.json"
    if not receipt_path.is_file():
        raise ArchiveError(f"Prepared reset receipt does not exist: {receipt_path}")
    try:
        payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ArchiveError(f"Cannot read prepared reset receipt: {exc}") from exc
    if not isinstance(payload, dict):
        raise ArchiveError("Prepared reset receipt is not a JSON object")
    return receipt_path, payload


def verify_reset_backup(
    path_value: Any,
    digest_value: Any,
    *,
    expected_path: Path,
    label: str,
) -> None:
    path = Path(str(path_value or ""))
    if not path.is_absolute() or path.resolve() != expected_path.resolve():
        raise ArchiveError(f"{label} backup path differs from the prepared request")
    if not path.is_file():
        raise ArchiveError(f"{label} backup does not exist: {path}")
    digest = str(digest_value or "")
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ArchiveError(f"{label} backup digest is invalid")
    if not secrets.compare_digest(file_sha256(path), digest):
        raise ArchiveError(f"{label} backup digest verification failed")
    with sqlite3.connect(path) as connection:
        row = connection.execute("PRAGMA integrity_check").fetchone()
    if not row or row[0] != "ok":
        raise ArchiveError(f"{label} backup integrity check failed: {row}")


def prepare_reset(
    config: ArchiveConfig,
    *,
    request_id: str,
    nonce: str,
    confirmation: str,
) -> dict[str, Any]:
    request_id = require_reset_request_id(request_id)
    nonce = require_reset_nonce(nonce)
    expected_confirmation = f"BACKUP {config.source_id} {request_id[:8]}"
    if not secrets.compare_digest(confirmation, expected_confirmation):
        raise ArchiveError("prepare-reset confirmation text is incorrect")
    archiver = Archiver(config)
    with archiver.lock():
        archiver.state.initialize()
        directory = reset_request_directory(config, request_id)
        receipt_path = directory / "receipt.json"
        islice_backup = directory / "islice-tasks.db"
        archive_backup = directory / "archive.db"
        if receipt_path.exists():
            _, receipt = load_reset_receipt(config, request_id)
            if str(receipt.get("nonce") or "") != nonce:
                raise ArchiveError("Existing reset receipt has a different nonce")
            if str(receipt.get("sourceId") or "") != config.source_id:
                raise ArchiveError("Existing reset receipt has a different source ID")
            verify_reset_backup(
                receipt.get("isliceDatabaseBackup"),
                receipt.get("isliceDatabaseSha256"),
                expected_path=islice_backup,
                label="iSlice database",
            )
            verify_reset_backup(
                receipt.get("archiveDatabaseBackup"),
                receipt.get("archiveDatabaseSha256"),
                expected_path=archive_backup,
                label="archive database",
            )
            return receipt
        if islice_backup.exists() or archive_backup.exists():
            raise ArchiveError(
                "Reset backup files already exist without a receipt; inspect them manually"
            )
        backup_sqlite(config.islice_database, islice_backup)
        backup_sqlite(config.state_database, archive_backup)
        receipt = {
            "requestId": request_id,
            "nonce": nonce,
            "sourceId": config.source_id,
            "preparedAt": iso_time(),
            "status": "prepared",
            "isliceDatabaseBackup": str(islice_backup),
            "isliceDatabaseSha256": file_sha256(islice_backup),
            "archiveDatabaseBackup": str(archive_backup),
            "archiveDatabaseSha256": file_sha256(archive_backup),
            "proof": secrets.token_urlsafe(32),
            "mediaDirectoriesBackedUp": False,
        }
        write_json_atomic(receipt_path, receipt)
        return receipt


def clear_islice_tasks(database: Path) -> None:
    with sqlite3.connect(database, timeout=30.0) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tasks'"
        ).fetchone()
        if table is None:
            raise ArchiveError("iSlice database has no tasks table")
        connection.execute("DELETE FROM tasks")
        sequence = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sqlite_sequence'"
        ).fetchone()
        if sequence is not None:
            connection.execute("DELETE FROM sqlite_sequence WHERE name='tasks'")
        row = connection.execute("PRAGMA integrity_check").fetchone()
        if not row or row[0] != "ok":
            raise ArchiveError(f"iSlice database integrity check failed after reset: {row}")


def clear_archive_state(database: Path) -> None:
    with sqlite3.connect(database, timeout=30.0) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        for table in ("archive_events", "archive_revisions", "archives"):
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            if exists is None:
                raise ArchiveError(f"Archive database has no {table} table")
            connection.execute(f"DELETE FROM {table}")
        sequence = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sqlite_sequence'"
        ).fetchone()
        if sequence is not None:
            connection.execute(
                "DELETE FROM sqlite_sequence WHERE name IN ('archive_events')"
            )
        row = connection.execute("PRAGMA integrity_check").fetchone()
        if not row or row[0] != "ok":
            raise ArchiveError(f"Archive database integrity check failed after reset: {row}")


def publish_empty_catalog(
    config: ArchiveConfig,
    state: StateStore,
    remote: Any | None = None,
) -> None:
    payload = state.catalog(config)
    path = config.manifest_root / "catalog.json"
    write_json_atomic(path, payload)
    (remote or RemoteArchive(config)).publish_catalog(path)


def commit_reset(
    config: ArchiveConfig,
    *,
    request_id: str,
    proof: str,
    confirmation: str,
    services_stopped: bool,
    remote: Any | None = None,
) -> dict[str, Any]:
    request_id = require_reset_request_id(request_id)
    if not services_stopped:
        raise ArchiveError("commit-reset requires --services-stopped")
    if not RESET_PROOF_PATTERN.fullmatch(proof):
        raise ArchiveError("Reset proof is invalid")
    expected_confirmation = f"RESET {config.source_id} {request_id[:8]}"
    if not secrets.compare_digest(confirmation, expected_confirmation):
        raise ArchiveError("commit-reset confirmation text is incorrect")
    archiver = Archiver(config, remote=remote)
    with archiver.lock():
        archiver.state.initialize()
        receipt_path, receipt = load_reset_receipt(config, request_id)
        if str(receipt.get("requestId") or "") != request_id:
            raise ArchiveError("Prepared reset receipt has a different request ID")
        if str(receipt.get("sourceId") or "") != config.source_id:
            raise ArchiveError("Prepared reset receipt has a different source ID")
        if not secrets.compare_digest(str(receipt.get("proof") or ""), proof):
            raise ArchiveError("Reset proof does not match the prepared receipt")
        directory = reset_request_directory(config, request_id)
        verify_reset_backup(
            receipt.get("isliceDatabaseBackup"),
            receipt.get("isliceDatabaseSha256"),
            expected_path=directory / "islice-tasks.db",
            label="iSlice database",
        )
        verify_reset_backup(
            receipt.get("archiveDatabaseBackup"),
            receipt.get("archiveDatabaseSha256"),
            expected_path=directory / "archive.db",
            label="archive database",
        )
        if receipt.get("status") == "committed":
            return receipt
        if receipt.get("status") not in {"prepared", "commit_ready"}:
            raise ArchiveError("Prepared reset receipt has an invalid status")

        final_islice = directory / "islice-tasks-final.db"
        final_archive = directory / "archive-final.db"
        if receipt.get("status") == "prepared":
            if final_islice.exists() or final_archive.exists():
                raise ArchiveError(
                    "Final reset backups exist without a commit-ready receipt; inspect manually"
                )
            backup_sqlite(config.islice_database, final_islice)
            backup_sqlite(config.state_database, final_archive)
            receipt.update(
                {
                    "status": "commit_ready",
                    "commitPreparedAt": iso_time(),
                    "finalIsliceDatabaseBackup": str(final_islice),
                    "finalIsliceDatabaseSha256": file_sha256(final_islice),
                    "finalArchiveDatabaseBackup": str(final_archive),
                    "finalArchiveDatabaseSha256": file_sha256(final_archive),
                }
            )
            write_json_atomic(receipt_path, receipt)
        verify_reset_backup(
            receipt.get("finalIsliceDatabaseBackup"),
            receipt.get("finalIsliceDatabaseSha256"),
            expected_path=final_islice,
            label="final iSlice database",
        )
        verify_reset_backup(
            receipt.get("finalArchiveDatabaseBackup"),
            receipt.get("finalArchiveDatabaseSha256"),
            expected_path=final_archive,
            label="final archive database",
        )

        clear_islice_tasks(config.islice_database)
        clear_archive_state(config.state_database)
        publish_empty_catalog(config, archiver.state, remote=archiver.remote)
        receipt.update(
            {
                "status": "committed",
                "committedAt": iso_time(),
                "mediaDirectoriesTouched": False,
            }
        )
        write_json_atomic(receipt_path, receipt)
        return receipt


def configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Archive completed iSlice task media")
    parser.add_argument("--version", action="version", version=ARCHIVER_AGENT_VERSION)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--verbose", action="store_true")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("run-once")
    commands.add_parser(
        "publish-catalog", help="publish the current catalog without scanning tasks"
    )
    forever_parser = commands.add_parser("run-forever")
    forever_parser.add_argument("--interval", type=float, default=300.0)
    status_parser = commands.add_parser("status")
    status_parser.add_argument("--json", action="store_true")
    retry_parser = commands.add_parser("retry")
    retry_parser.add_argument("task_id")
    prepare_parser = commands.add_parser(
        "prepare-reset", help="back up databases and issue a reset receipt"
    )
    prepare_parser.add_argument("--request-id", required=True)
    prepare_parser.add_argument("--nonce", required=True)
    prepare_parser.add_argument("--confirm", required=True)
    prepare_parser.add_argument("--json", action="store_true")
    commit_parser = commands.add_parser(
        "commit-reset", help="clear database state after a prepared reset"
    )
    commit_parser.add_argument("--request-id", required=True)
    commit_parser.add_argument("--proof", required=True)
    commit_parser.add_argument("--confirm", required=True)
    commit_parser.add_argument("--services-stopped", action="store_true")
    args = parser.parse_args(argv)
    configure_logging(args.verbose)

    try:
        config = ArchiveConfig.from_file(args.config)
        if args.command == "prepare-reset":
            receipt = prepare_reset(
                config,
                request_id=args.request_id,
                nonce=args.nonce,
                confirmation=args.confirm,
            )
            if args.json:
                print(json.dumps(receipt, ensure_ascii=False))
            else:
                print(json.dumps(receipt, ensure_ascii=False, indent=2))
            return 0
        if args.command == "commit-reset":
            receipt = commit_reset(
                config,
                request_id=args.request_id,
                proof=args.proof,
                confirmation=args.confirm,
                services_stopped=args.services_stopped,
            )
            print(json.dumps(receipt, ensure_ascii=False, indent=2))
            return 0
        state = StateStore(config.state_database, config.source_id)
        state.initialize()
        if args.command == "run-once":
            Archiver(config, state=state).run_once()
        elif args.command == "publish-catalog":
            Archiver(config, state=state).publish_catalog()
        elif args.command == "run-forever":
            try:
                Archiver(config, state=state).run_forever(args.interval)
            except KeyboardInterrupt:
                logger.info("Archive agent stopped")
        elif args.command == "retry":
            state.retry(require_task_id(args.task_id))
        elif args.command == "status":
            rows = state.list()
            if args.json:
                print(json.dumps(rows, ensure_ascii=False, indent=2))
            else:
                for row in rows:
                    print(
                        f"{row['task_id']}\t{row['state']}\t{row['file_count']} files\t"
                        f"{row['total_bytes']} bytes\t{row['error_message']}"
                    )
        return 0
    except (ArchiveError, OSError, sqlite3.Error) as exc:
        logger.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
