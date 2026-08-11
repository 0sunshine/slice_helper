from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import sqlite3
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any


REQUEST_ID_PATTERN = re.compile(r"[0-9a-f]{32}")
NONCE_PATTERN = re.compile(r"[A-Za-z0-9_-]{20,128}")
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
PROOF_PATTERN = re.compile(r"[A-Za-z0-9_-]{20,128}")


class SystemResetError(RuntimeError):
    pass


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
        raise SystemResetError(f"SQLite database does not exist: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".partial")
    if temporary.exists():
        temporary.unlink()
    source_connection: sqlite3.Connection | None = None
    destination_connection: sqlite3.Connection | None = None
    try:
        source_connection = sqlite3.connect(source, timeout=30.0)
        destination_connection = sqlite3.connect(temporary)
        source_connection.backup(destination_connection)
        row = destination_connection.execute("PRAGMA integrity_check").fetchone()
        if not row or row[0] != "ok":
            raise SystemResetError(f"SQLite backup integrity check failed: {row}")
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


def create_helper_backup(
    database_path: Path,
    data_dir: Path,
    request_id: str,
) -> dict[str, Any]:
    if not REQUEST_ID_PATTERN.fullmatch(request_id):
        raise SystemResetError("Invalid reset request ID")
    backup_root = (data_dir.resolve() / "reset-backups" / request_id).resolve()
    try:
        backup_root.relative_to(data_dir.resolve())
    except ValueError as exc:
        raise SystemResetError("Reset backup path escaped DATA_DIR") from exc
    backup_path = backup_root / "slice-helper.db"
    backup_sqlite(database_path, backup_path)
    result = {
        "component": "slice_helper",
        "requestId": request_id,
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "databaseBackup": str(backup_path),
        "sha256": file_sha256(backup_path),
        "mediaDirectoriesBackedUp": False,
    }
    manifest = backup_root / "backup.json"
    temporary = manifest.with_suffix(".json.partial")
    temporary.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temporary, manifest)
    return result


def validate_agent_receipts(
    receipts: list[dict[str, Any]],
    *,
    request_id: str,
    nonce: str,
    required_source_ids: set[str],
) -> list[dict[str, Any]]:
    by_source: dict[str, dict[str, Any]] = {}

    def absolute_on_either_platform(value: str) -> bool:
        return PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute()

    for receipt in receipts:
        source_id = str(receipt.get("sourceId") or "")
        if source_id in by_source:
            raise SystemResetError(f"Duplicate reset receipt for {source_id}")
        if str(receipt.get("requestId") or "") != request_id:
            raise SystemResetError(f"Receipt requestId differs for {source_id or 'unknown'}")
        if str(receipt.get("nonce") or "") != nonce:
            raise SystemResetError(f"Receipt nonce differs for {source_id or 'unknown'}")
        if not source_id or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", source_id):
            raise SystemResetError("Receipt contains an invalid sourceId")
        for field in ("isliceDatabaseSha256", "archiveDatabaseSha256"):
            if not SHA256_PATTERN.fullmatch(str(receipt.get(field) or "")):
                raise SystemResetError(f"Receipt {source_id} has invalid {field}")
        if not PROOF_PATTERN.fullmatch(str(receipt.get("proof") or "")):
            raise SystemResetError(f"Receipt {source_id} has an invalid proof")
        if receipt.get("status") != "prepared":
            raise SystemResetError(f"Receipt {source_id} is not in prepared state")
        try:
            prepared_at = datetime.fromisoformat(str(receipt.get("preparedAt") or ""))
        except ValueError as exc:
            raise SystemResetError(f"Receipt {source_id} has invalid preparedAt") from exc
        if prepared_at.tzinfo is None:
            raise SystemResetError(f"Receipt {source_id} preparedAt lacks a timezone")
        if not absolute_on_either_platform(
            str(receipt.get("isliceDatabaseBackup") or "")
        ):
            raise SystemResetError(f"Receipt {source_id} lacks an absolute iSlice backup path")
        if not absolute_on_either_platform(
            str(receipt.get("archiveDatabaseBackup") or "")
        ):
            raise SystemResetError(f"Receipt {source_id} lacks an absolute archive backup path")
        if receipt.get("mediaDirectoriesBackedUp") is not False:
            raise SystemResetError(f"Receipt {source_id} has an unexpected media backup flag")
        by_source[source_id] = receipt
    missing = required_source_ids - set(by_source)
    extra = set(by_source) - required_source_ids
    if missing:
        raise SystemResetError(f"Missing reset receipt(s): {', '.join(sorted(missing))}")
    if extra:
        raise SystemResetError(f"Unexpected reset receipt(s): {', '.join(sorted(extra))}")
    return [by_source[source_id] for source_id in sorted(by_source)]


def prepare_agent_command(
    source_id: str, request_id: str, nonce: str, confirmation: str, install_path: str
) -> str:
    root = install_path.rstrip("/") or "/opt/islice-archiver"
    return (
        f"python3 {shlex.quote(root + '/islice_archiver.py')} "
        f"--config {shlex.quote(root + '/islice-archiver.ini')} prepare-reset "
        f"--request-id {request_id} --nonce {nonce} "
        f"--confirm '{confirmation}' --json"
    )


def commit_agent_command(
    receipt: dict[str, Any], confirmation: str, install_path: str
) -> str:
    root = install_path.rstrip("/") or "/opt/islice-archiver"
    return (
        f"python3 {shlex.quote(root + '/islice_archiver.py')} "
        f"--config {shlex.quote(root + '/islice-archiver.ini')} commit-reset "
        f"--request-id {receipt['requestId']} --proof {receipt['proof']} "
        f"--confirm '{confirmation}' --services-stopped"
    )
