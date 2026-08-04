from __future__ import annotations

import argparse
import configparser
import contextlib
import hashlib
import json
import logging
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
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
TASK_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
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
    delete_delay_hours: float = 24.0
    retry_delay_minutes: float = 30.0
    max_tasks_per_run: int = 4
    command_timeout_seconds: float = 21600.0
    http_timeout_seconds: float = 30.0

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
            delete_delay_hours=section.getfloat("delete_delay_hours", 24.0),
            retry_delay_minutes=section.getfloat("retry_delay_minutes", 30.0),
            max_tasks_per_run=section.getint("max_tasks_per_run", 4),
            command_timeout_seconds=section.getfloat("command_timeout_seconds", 21600.0),
            http_timeout_seconds=section.getfloat("http_timeout_seconds", 30.0),
        )
        if config.delete_delay_hours < 0:
            raise ArchiveError("delete_delay_hours must not be negative")
        if config.max_tasks_per_run < 1:
            raise ArchiveError("max_tasks_per_run must be positive")
        if not config.remote_root.startswith("/"):
            raise ArchiveError("remote_root must be absolute")
        if not config.remote_http_base.startswith(("http://", "https://")):
            raise ArchiveError("remote_http_base must use HTTP or HTTPS")
        return config


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
    def __init__(self, path: Path):
        self.path = path

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
                """
            )
            connection.execute(
                "UPDATE archives SET state='pending', error_message='Recovered after interruption' "
                "WHERE state IN ('syncing', 'verifying')"
            )

    def discover(self, task_id: str, task_path: Path, output_path: Path, remote_path: str) -> None:
        now = iso_time()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO archives (
                    task_id, state, local_task_path, local_output_path, remote_path,
                    discovered_at, updated_at
                ) VALUES (?, 'pending', ?, ?, ?, ?, ?)
                """,
                (task_id, str(task_path), str(output_path), remote_path, now, now),
            )
            if cursor.rowcount:
                self._event(connection, task_id, "discovered", str(output_path))

    def candidates(self, limit: int) -> list[dict[str, Any]]:
        now = iso_time()
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM archives
                WHERE state='pending'
                   OR (state='failed' AND (next_attempt_at IS NULL OR next_attempt_at <= ?))
                ORDER BY discovered_at
                LIMIT ?
                """,
                (now, limit),
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

    def sync(
        self,
        manifest: TaskManifest,
        output_dir: Path,
        manifest_json: Path,
        manifest_checksums: Path,
    ) -> None:
        task_id = require_task_id(manifest.task_id)
        staging = f"{self.config.remote_root}/incoming/{task_id}.partial"
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
            if existing_digest != manifest.digest:
                raise ArchiveError(f"Remote archive conflict for {task_id}")
            self.verify(manifest)
            return

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
            "set -eu; "
            f"cd {shlex.quote(staging)}; "
            "sha256sum -c manifest.sha256 >/dev/null; "
            f"test ! -e {shlex.quote(final)}; "
            f"mv {shlex.quote(staging)} {shlex.quote(final)}"
        )
        self.verify(manifest)

    def verify(self, manifest: TaskManifest) -> None:
        final = f"{self.config.remote_root}/tasks/{require_task_id(manifest.task_id)}"
        self.ssh(
            f"set -eu; cd {shlex.quote(final)}; sha256sum -c manifest.sha256 >/dev/null"
        )
        for item in manifest.media_files:
            self._verify_http(manifest.task_id, item)

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
        self.state = state or StateStore(config.state_database)
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
            self.discover()
            self.delete_due()
            candidates = self.state.candidates(self.config.max_tasks_per_run)
            logger.info("Archive scan found %d task(s) ready for processing", len(candidates))
            for row in candidates:
                self.archive(row)

    def discover(self) -> None:
        for task, task_path, output_path in self.tasks.completed():
            task_id = require_task_id(str(task["task_id"]))
            self.state.discover(
                task_id,
                task_path,
                output_path,
                f"{self.config.remote_root}/tasks/{task_id}",
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
            self.state.transition(
                task_id,
                "syncing",
                "sync_started",
                manifest_digest=manifest.digest,
                file_count=len(manifest.files),
                total_bytes=manifest.total_bytes,
                deletion_eligible=int(manifest.deletion_eligible),
                warnings_json=json.dumps(manifest.warnings, ensure_ascii=False),
                error_message="",
            )
            self.remote.sync(manifest, output_path, manifest_json, manifest_checksums)
            self.state.transition(task_id, "verifying", "remote_verified")
            archived_at = utc_now()
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


def configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Archive completed iSlice task media")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--verbose", action="store_true")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("run-once")
    status_parser = commands.add_parser("status")
    status_parser.add_argument("--json", action="store_true")
    retry_parser = commands.add_parser("retry")
    retry_parser.add_argument("task_id")
    args = parser.parse_args(argv)
    configure_logging(args.verbose)

    try:
        config = ArchiveConfig.from_file(args.config)
        state = StateStore(config.state_database)
        state.initialize()
        if args.command == "run-once":
            Archiver(config, state=state).run_once()
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
    except ArchiveError as exc:
        logger.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
