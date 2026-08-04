from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from slice_helper.archive_agent import (
    ArchiveConfig,
    Archiver,
    StateStore,
    TaskManifest,
    build_manifest,
)


def make_task_database(path: Path, task_id: str = "task-001") -> None:
    connection = sqlite3.connect(path)
    connection.execute(
        """
        CREATE TABLE tasks (
            task_id TEXT PRIMARY KEY,
            video_path TEXT NOT NULL,
            template_id TEXT DEFAULT '',
            channel_name TEXT DEFAULT '',
            program_start_time TEXT DEFAULT '',
            output_dir TEXT DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            start_time TEXT DEFAULT '',
            end_time TEXT DEFAULT '',
            error_message TEXT DEFAULT '',
            progress INTEGER NOT NULL DEFAULT 0,
            language TEXT NOT NULL DEFAULT 'zh',
            create_time TEXT NOT NULL,
            modify_time TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        INSERT INTO tasks (
            task_id,video_path,status,progress,create_time,modify_time,end_time
        ) VALUES (?, 'http://helper/chunk.ts', 'completed', 100, ?, ?, ?)
        """,
        (task_id, "2026-08-04T00:00:00Z", "2026-08-04T01:00:00Z", "2026-08-04T01:00:00Z"),
    )
    connection.commit()
    connection.close()


def make_output(
    storage: Path, task_id: str = "task-001", *, empty_last_url: bool = False
) -> Path:
    task_dir = storage / task_id
    output = task_dir / "output"
    segments = output / "segments"
    covers = output / "covers"
    segments.mkdir(parents=True)
    covers.mkdir(parents=True)
    (task_dir / "video").mkdir()
    (task_dir / "video" / "original.mp4").write_bytes(b"source")
    (task_dir / "temp").mkdir()
    (task_dir / "temp" / "working.json").write_text("{}", encoding="utf-8")
    (segments / "one.mp4").write_bytes(b"video-one")
    (covers / "one.jpg").write_bytes(b"cover-one")
    result_segments = [
        {
            "startTime": 0,
            "endTime": 10,
            "segmentUrl": f"http://islice/download/{task_id}/segments/one.mp4",
            "coverImgUrl": f"http://islice/download/{task_id}/covers/one.jpg",
        }
    ]
    if empty_last_url:
        result_segments.append(
            {
                "startTime": 10,
                "endTime": 20,
                "segmentUrl": "",
                "coverImgUrl": "",
            }
        )
    (output / "segments.json").write_text(
        json.dumps({"segments": result_segments}), encoding="utf-8"
    )
    (output / "metadata.json").write_text("{}", encoding="utf-8")
    return output


def task_payload(task_id: str = "task-001") -> dict[str, object]:
    return {
        "task_id": task_id,
        "video_path": "http://helper/chunk.ts",
        "status": "completed",
    }


def make_config(tmp_path: Path, *, delete_delay_hours: float = 24) -> ArchiveConfig:
    return ArchiveConfig(
        islice_database=tmp_path / "tasks.db",
        storage_root=tmp_path / "storage",
        state_database=tmp_path / "state" / "archive.db",
        manifest_root=tmp_path / "state" / "manifests",
        lock_path=tmp_path / "state" / "archive.lock",
        remote_host="archive.test",
        remote_user="codex",
        remote_root="/archive",
        remote_http_base="http://archive.test",
        ssh_key=tmp_path / "id_ed25519",
        known_hosts=tmp_path / "known_hosts",
        delete_delay_hours=delete_delay_hours,
    )


class FakeRemote:
    def __init__(self) -> None:
        self.synced: list[TaskManifest] = []
        self.verified: list[TaskManifest] = []

    def sync(self, manifest, _output, _manifest_json, _manifest_checksums) -> None:
        self.synced.append(manifest)

    def verify(self, manifest) -> None:
        self.verified.append(manifest)


def test_manifest_hashes_all_output_and_is_deletion_eligible(tmp_path: Path) -> None:
    output = make_output(tmp_path / "storage")

    manifest = build_manifest(task_payload(), output)

    assert manifest.deletion_eligible
    assert not manifest.warnings
    assert {item.path for item in manifest.files} == {
        "covers/one.jpg",
        "metadata.json",
        "segments.json",
        "segments/one.mp4",
    }
    assert manifest.total_bytes == sum(item.size for item in manifest.files)
    assert len(manifest.digest) == 64


def test_empty_result_url_archives_with_deletion_hold(tmp_path: Path) -> None:
    output = make_output(tmp_path / "storage", empty_last_url=True)

    manifest = build_manifest(task_payload(), output)

    assert not manifest.deletion_eligible
    assert "Segment 2 has an empty segmentUrl" in manifest.warnings
    assert "Segment 2 has an empty coverImgUrl" in manifest.warnings


def test_archive_then_delayed_delete_keeps_marker(tmp_path: Path) -> None:
    config = make_config(tmp_path, delete_delay_hours=0)
    make_task_database(config.islice_database)
    output = make_output(config.storage_root)
    state = StateStore(config.state_database)
    remote = FakeRemote()
    archiver = Archiver(config, state=state, remote=remote)  # type: ignore[arg-type]

    archiver.run_once()

    archived = state.get("task-001")
    assert archived is not None
    assert archived["state"] == "delete_pending"
    assert len(remote.synced) == 1
    assert output.is_dir()

    archiver.run_once()

    deleted = state.get("task-001")
    assert deleted is not None
    assert deleted["state"] == "deleted"
    task_dir = config.storage_root / "task-001"
    assert not (task_dir / "output").exists()
    assert not (task_dir / "video").exists()
    assert not (task_dir / "temp").exists()
    marker = json.loads((task_dir / "archive.json").read_text(encoding="utf-8"))
    assert marker["taskId"] == "task-001"
    assert marker["manifestDigest"] == remote.synced[0].digest


def test_incomplete_task_is_archived_without_local_deletion(tmp_path: Path) -> None:
    config = make_config(tmp_path, delete_delay_hours=0)
    make_task_database(config.islice_database)
    output = make_output(config.storage_root, empty_last_url=True)
    state = StateStore(config.state_database)
    remote = FakeRemote()
    archiver = Archiver(config, state=state, remote=remote)  # type: ignore[arg-type]

    archiver.run_once()
    archiver.run_once()

    archived = state.get("task-001")
    assert archived is not None
    assert archived["state"] == "archived_hold"
    assert output.is_dir()
    assert len(remote.synced) == 1
