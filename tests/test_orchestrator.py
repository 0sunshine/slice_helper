from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from slice_helper.config import Settings
from slice_helper.database import Database
from slice_helper.islice import ISliceError, ISlicePool
from slice_helper.media import MediaError
from slice_helper.models import MediaProbe
from slice_helper.orchestrator import Orchestrator


class FakeMedia:
    async def cut(self, _source, target, _start, _end, _mode):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"fake-ts")
        return MediaProbe(duration=3600, format_name="mpegts", video_codec="h264")


class InvalidMedia:
    async def cut(self, _source, _target, _start, _end, _mode):
        raise MediaError("chunk failed validation after audio repair")


class FakeISlice:
    async def ensure_task(self, _task_id, _request):
        return None

    async def get_task_info(self, task_id):
        if "w000" in task_id:
            segments = [
                {"startTime": 0, "endTime": 3500, "title": "first"},
                {"startTime": 3500, "endTime": 3600, "title": "handoff"},
            ]
        else:
            segments = [
                {"startTime": 0, "endTime": 100, "title": "handoff-redone"},
                {"startTime": 100, "endTime": 3700, "title": "last"},
            ]
        return {
            "taskInfo": {"taskId": task_id, "status": "completed", "videoPath": "unused"},
            "metaData": {},
            "segments": segments,
        }


class PausingISlice:
    def __init__(self, database: Database, job_id: str):
        self.database = database
        self.job_id = job_id
        self.resume = False

    async def ensure_task(self, _task_id, _request):
        return None

    async def get_task_info(self, task_id):
        if not self.resume:
            await self.database.update_job(
                self.job_id, status="pause_requested", pause_requested=1
            )
            return {
                "taskInfo": {
                "taskId": task_id,
                "status": "processing",
                "progress": 37,
                "videoPath": "unused",
                },
                "segments": [],
            }
        return {
            "taskInfo": {
                "taskId": task_id,
                "status": "completed",
                "progress": 100,
                "videoPath": "unused",
            },
            "segments": [{"startTime": 0, "endTime": 3600, "title": "done"}],
        }


class FailingISlice:
    def __init__(self):
        self.calls = 0

    async def ensure_task(self, _task_id, _request):
        self.calls += 1
        raise ISliceError("service unavailable")


class OverlappingISlice:
    async def ensure_task(self, _task_id, _request):
        return None

    async def get_task_info(self, task_id):
        segments = (
            [{"startTime": 0, "endTime": 3620, "title": "long"}]
            if "w000" in task_id
            else [{"startTime": 0, "endTime": 3600, "title": "overlap"}]
        )
        return {
            "taskInfo": {
                "taskId": task_id,
                "status": "completed",
                "videoPath": "unused",
            },
            "segments": segments,
        }


def make_settings(tmp_path: Path, duration_timeout: float = 2) -> Settings:
    return Settings(
        islice_base_url="http://islice.test",
        public_base_url="http://helper.test",
        host="127.0.0.1",
        port=8090,
        data_dir=tmp_path / "data",
        temp_dir=tmp_path / "temp",
        ffmpeg_path="ffmpeg",
        ffprobe_path="ffprobe",
        max_active_jobs=1,
        poll_interval_seconds=0.001,
        window_timeout_seconds=duration_timeout,
        ffmpeg_timeout_seconds=10,
    )


async def create_job(database: Database, source: Path, *, duration: float) -> None:
    stat = source.stat()
    await database.create_job(
        {
            "id": "abc123",
            "status": "queued",
            "source_path": str(source),
            "source_size": stat.st_size,
            "source_mtime_ns": stat.st_mtime_ns,
            "source_duration": duration,
            "template_id": "general",
            "language": "zh",
            "channel_name": "",
            "program_start_time": None,
            "cut_mode": "copy",
            "total_windows": int(duration / 3600),
        }
    )


@pytest.mark.asyncio
async def test_two_window_job_hands_tail_to_next_window(tmp_path: Path) -> None:
    source = tmp_path / "source.ts"
    source.write_bytes(b"source")
    stat = source.stat()
    configured = Settings(
        islice_base_url="http://islice.test",
        public_base_url="http://helper.test",
        host="127.0.0.1",
        port=8090,
        data_dir=tmp_path / "data",
        temp_dir=tmp_path / "temp",
        ffmpeg_path="ffmpeg",
        ffprobe_path="ffprobe",
        max_active_jobs=1,
        poll_interval_seconds=0.001,
        window_timeout_seconds=2,
        ffmpeg_timeout_seconds=10,
    )
    database = Database(configured.database_path)
    await database.initialize()
    await database.create_job(
        {
            "id": "abc123",
            "status": "queued",
            "source_path": str(source),
            "source_size": stat.st_size,
            "source_mtime_ns": stat.st_mtime_ns,
            "source_duration": 7200.0,
            "template_id": "general",
            "language": "zh",
            "channel_name": "",
            "program_start_time": None,
            "cut_mode": "copy",
            "total_windows": 2,
        }
    )
    orchestrator = Orchestrator(configured, database, FakeMedia(), FakeISlice())
    await orchestrator._process_job("abc123")

    job = await database.get_job("abc123")
    assert job["status"] == "completed"
    windows = await database.get_windows("abc123")
    assert windows[1]["requested_start"] == 3500
    segments = await database.get_segments("abc123")
    assert [item["accepted"] for item in segments] == [1, 0, 1, 1]
    assert segments[2]["global_start"] == 3500
    manifest = json.loads(
        (configured.data_dir / "jobs" / "abc123" / "result.json").read_text(encoding="utf-8")
    )
    assert manifest["job"]["status"] == "completed"
    assert len(manifest["segments"]) == 4
    assert not list(configured.temp_dir.rglob("*.ts"))


@pytest.mark.asyncio
async def test_pause_is_honored_after_poll_and_resume_reuses_attempt(tmp_path: Path) -> None:
    source = tmp_path / "source.ts"
    source.write_bytes(b"source")
    configured = make_settings(tmp_path)
    database = Database(configured.database_path)
    await database.initialize()
    await create_job(database, source, duration=3600)
    islice = PausingISlice(database, "abc123")
    orchestrator = Orchestrator(configured, database, FakeMedia(), islice)

    await orchestrator._process_job("abc123")

    job = await database.get_job("abc123")
    assert job["status"] == "paused"
    window = (await database.get_windows("abc123"))[0]
    assert window["status"] == "polling"
    assert Path(window["chunk_path"]).is_file()
    attempts = await database.get_attempts(window["id"])
    assert len(attempts) == 1
    assert attempts[0]["status"] == "polling"
    assert attempts[0]["service_status"] == "processing"
    assert attempts[0]["progress"] == 37

    islice.resume = True
    await database.update_job("abc123", status="queued", pause_requested=0)
    await orchestrator._process_job("abc123")

    assert (await database.get_job("abc123"))["status"] == "completed"
    attempts = await database.get_attempts(window["id"])
    assert len(attempts) == 1
    assert attempts[0]["service_status"] == "completed"
    assert attempts[0]["progress"] == 100


@pytest.mark.asyncio
async def test_initial_attempt_plus_three_retries_then_pause(tmp_path: Path) -> None:
    source = tmp_path / "source.ts"
    source.write_bytes(b"source")
    configured = make_settings(tmp_path)
    database = Database(configured.database_path)
    await database.initialize()
    await create_job(database, source, duration=3600)
    islice = FailingISlice()
    orchestrator = Orchestrator(configured, database, FakeMedia(), islice)
    orchestrator.RETRY_DELAYS = (0.0, 0.0, 0.0)

    await orchestrator._process_job("abc123")

    job = await database.get_job("abc123")
    window = (await database.get_windows("abc123"))[0]
    assert job["status"] == "paused"
    assert "4 attempts" in job["error_message"]
    assert islice.calls == 4
    assert len(await database.get_attempts(window["id"])) == 4
    assert Path(window["chunk_path"]).is_file()

    await database.update_job("abc123", status="queued", pause_requested=0)
    await orchestrator._process_job("abc123")

    assert (await database.get_job("abc123"))["status"] == "paused"
    assert islice.calls == 8
    assert len(await database.get_attempts(window["id"])) == 8


@pytest.mark.asyncio
async def test_invalid_chunk_pauses_before_islice_submission(tmp_path: Path) -> None:
    source = tmp_path / "source.ts"
    source.write_bytes(b"source")
    configured = make_settings(tmp_path)
    database = Database(configured.database_path)
    await database.initialize()
    await create_job(database, source, duration=3600)
    islice = FailingISlice()
    orchestrator = Orchestrator(configured, database, InvalidMedia(), islice)

    await orchestrator._process_job("abc123")

    job = await database.get_job("abc123")
    window = (await database.get_windows("abc123"))[0]
    assert job["status"] == "paused"
    assert "media preparation failed" in job["error_message"]
    assert window["status"] == "failed"
    assert "audio repair" in window["error_message"]
    assert islice.calls == 0
    assert not await database.get_attempts(window["id"])


@pytest.mark.asyncio
async def test_cross_window_overlap_pauses_without_committing_new_segments(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.ts"
    source.write_bytes(b"source")
    configured = make_settings(tmp_path)
    database = Database(configured.database_path)
    await database.initialize()
    await create_job(database, source, duration=7200)
    orchestrator = Orchestrator(configured, database, FakeMedia(), OverlappingISlice())

    await orchestrator._process_job("abc123")

    job = await database.get_job("abc123")
    windows = await database.get_windows("abc123")
    assert job["status"] == "paused"
    assert "overlap the previous window" in job["error_message"]
    assert windows[0]["status"] == "completed"
    assert windows[1]["status"] == "failed"
    assert len(await database.get_segments("abc123")) == 1


@pytest.mark.asyncio
async def test_all_windows_stay_on_the_job_assigned_islice(tmp_path: Path) -> None:
    source = tmp_path / "source.ts"
    source.write_bytes(b"source")
    configured = make_settings(tmp_path)
    configured = replace(
        configured,
        islice_base_urls=("http://islice-a.test", "http://islice-b.test"),
    )
    database = Database(configured.database_path)
    await database.initialize()
    await create_job(database, source, duration=7200)
    await database.update_job("abc123", islice_base_url="http://islice-b.test")

    selected = FakeISlice()

    class UnexpectedISlice:
        async def ensure_task(self, _task_id, _request):
            raise AssertionError("job was dispatched to the wrong iSlice")

    pool = ISlicePool(configured)
    await pool.close()
    pool.clients = {
        "http://islice-a.test": UnexpectedISlice(),
        "http://islice-b.test": selected,
    }
    orchestrator = Orchestrator(configured, database, FakeMedia(), pool)
    await orchestrator._process_job("abc123")

    job = await database.get_job("abc123")
    assert job["status"] == "completed"
    assert job["islice_base_url"] == "http://islice-b.test"
    attempts = await database.get_attempts_for_job("abc123")
    assert [attempt["window_index"] for attempt in attempts] == [0, 1]


@pytest.mark.asyncio
async def test_job_pauses_instead_of_moving_from_removed_islice(tmp_path: Path) -> None:
    source = tmp_path / "source.ts"
    source.write_bytes(b"source")
    configured = make_settings(tmp_path)
    database = Database(configured.database_path)
    await database.initialize()
    await create_job(database, source, duration=3600)
    await database.update_job("abc123", islice_base_url="http://removed-islice.test")
    await database.update_job("abc123", status="pending_schedule")
    assert not await database.claim_schedulable_jobs(
        configured.configured_islice_urls, 1
    )

    job = await database.get_job("abc123")
    assert job["status"] == "paused"
    assert "unconfigured iSlice instance" in job["error_message"]
    assert not await database.get_windows("abc123")
