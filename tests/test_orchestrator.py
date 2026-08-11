from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest

from slice_helper.config import Settings
from slice_helper.database import Database
from slice_helper.islice import ISliceError, ISlicePool
from slice_helper.media import MediaError
from slice_helper.models import MediaProbe
from slice_helper.orchestrator import (
    Orchestrator,
    ResplitConflictError,
    _ISliceSubmissionGate,
)


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
                {"startTime": 0, "endTime": 3500, "title": "first", "contentType": "新闻"},
                {"startTime": 3500, "endTime": 3600, "title": "handoff", "contentType": "广告"},
            ]
        else:
            segments = [
                {"startTime": 0, "endTime": 100, "title": "handoff-redone", "contentType": "广告"},
                {"startTime": 100, "endTime": 3700, "title": "last", "contentType": "电视剧"},
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
        self.pause_triggered = False

    async def ensure_task(self, _task_id, _request):
        return None

    async def get_task_info(self, task_id):
        if not self.resume and not self.pause_triggered:
            self.pause_triggered = True
            await self.database.update_job(
                self.job_id, status="pause_requested", pause_requested=1
            )
        if not self.resume:
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


class TerminalFailingISlice:
    def __init__(self):
        self.calls = 0

    async def ensure_task(self, _task_id, _request):
        self.calls += 1
        return None

    async def get_task_info(self, task_id):
        return {
            "taskInfo": {
                "taskId": task_id,
                "status": "failed",
                "progress": 100,
                "videoPath": "unused",
                "errorMessage": "internal retries exhausted",
            },
            "segments": [],
        }


class ResplitISlice:
    def __init__(self, *, terminal_status: str = "completed") -> None:
        self.terminal_status = terminal_status
        self.deleted: list[str] = []
        self.created: list[tuple[str, dict]] = []

    async def delete_task(self, task_id):
        self.deleted.append(task_id)
        return True

    async def ensure_task(self, task_id, request):
        self.created.append((task_id, request))
        return None

    async def get_task_info(self, task_id):
        payload = {
            "taskInfo": {
                "taskId": task_id,
                "status": self.terminal_status,
                "progress": 100,
                "videoPath": "unused",
            },
            "segments": [],
        }
        if self.terminal_status == "completed":
            payload["segments"] = [
                {
                    "startTime": 0,
                    "endTime": 3600,
                    "title": "replacement",
                    "contentType": "新闻",
                }
            ]
        else:
            payload["taskInfo"]["errorMessage"] = "manual resplit failed"
        return payload


class BlockingResplitISlice(ResplitISlice):
    def __init__(self) -> None:
        super().__init__()
        self.delete_started = asyncio.Event()
        self.allow_delete = asyncio.Event()

    async def delete_task(self, task_id):
        self.deleted.append(task_id)
        self.delete_started.set()
        await self.allow_delete.wait()
        return True


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


class ControlledISlice:
    def __init__(self) -> None:
        self.created: asyncio.Queue[str] = asyncio.Queue()
        self.states: dict[str, tuple[str, float]] = {}

    async def ensure_task(self, task_id, request):
        if task_id not in self.states:
            self.states[task_id] = ("processing", 0.0)
            self.created.put_nowait(task_id)
        return self._payload(task_id, request["videoPath"])

    async def get_task_info(self, task_id):
        return self._payload(task_id, "unused")

    def set_state(self, task_id: str, status: str, progress: float) -> None:
        self.states[task_id] = (status, progress)

    def _payload(self, task_id: str, video_path: str) -> dict:
        status, progress = self.states[task_id]
        payload = {
            "taskInfo": {
                "taskId": task_id,
                "status": status,
                "progress": progress,
                "videoPath": video_path,
            },
            "segments": [],
        }
        if status == "completed":
            payload["segments"] = [
                {"startTime": 0, "endTime": 3600, "title": task_id}
            ]
        return payload


class CapacityISlice:
    def __init__(self, capacity: int) -> None:
        self.capacity = capacity
        self.created: asyncio.Queue[str] = asyncio.Queue()
        self.states: dict[str, tuple[str, float]] = {}
        self.video_paths: dict[str, str] = {}
        self.creation_order: list[str] = []
        self.max_processing = 0

    async def close(self) -> None:
        return None

    async def ensure_task(self, task_id, request):
        if task_id not in self.states:
            status = (
                "processing"
                if self.processing_count < self.capacity
                else "pending"
            )
            self.states[task_id] = (status, 0.0)
            self.video_paths[task_id] = request["videoPath"]
            self.creation_order.append(task_id)
            self.created.put_nowait(task_id)
            self._record_processing()
        return self._payload(task_id)

    async def get_task_info(self, task_id):
        return self._payload(task_id)

    @property
    def processing_count(self) -> int:
        return sum(status == "processing" for status, _progress in self.states.values())

    def set_progress(self, task_id: str, progress: float) -> None:
        status, _current = self.states[task_id]
        if status != "processing":
            raise AssertionError(f"Cannot advance non-processing task {task_id}: {status}")
        self.states[task_id] = (status, progress)

    def complete(self, task_id: str) -> None:
        status, _progress = self.states[task_id]
        if status != "processing":
            raise AssertionError(f"Cannot complete non-processing task {task_id}: {status}")
        self.states[task_id] = ("completed", 100.0)
        for candidate in self.creation_order:
            candidate_status, _candidate_progress = self.states[candidate]
            if candidate_status == "pending":
                self.states[candidate] = ("processing", 0.0)
                break
        self._record_processing()

    def _record_processing(self) -> None:
        self.max_processing = max(self.max_processing, self.processing_count)
        if self.processing_count > self.capacity:
            raise AssertionError("Simulated iSlice exceeded its concurrency capacity")

    def _payload(self, task_id: str) -> dict:
        status, progress = self.states[task_id]
        payload = {
            "taskInfo": {
                "taskId": task_id,
                "status": status,
                "progress": progress,
                "videoPath": self.video_paths[task_id],
            },
            "segments": [],
        }
        if status == "completed":
            payload["segments"] = [
                {"startTime": 0, "endTime": 3600, "title": task_id}
            ]
        return payload


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


async def create_named_job(
    database: Database,
    source: Path,
    job_id: str,
    duration: float,
) -> None:
    stat = source.stat()
    await database.create_job(
        {
            "id": job_id,
            "status": "queued",
            "islice_base_url": "http://islice.test",
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


async def wait_for_current_window(
    database: Database, job_id: str, expected: int
) -> None:
    for _ in range(500):
        job = await database.get_job(job_id)
        if job and int(job["current_window"]) == expected:
            return
        await asyncio.sleep(0.002)
    raise AssertionError(f"{job_id} did not advance to window {expected}")


async def wait_for_attempt(
    database: Database, job_id: str, window_index: int
) -> None:
    for _ in range(500):
        attempts = await database.get_attempts_for_job(job_id)
        if any(int(item["window_index"]) == window_index for item in attempts):
            await asyncio.sleep(0.01)
            return
        await asyncio.sleep(0.002)
    raise AssertionError(f"{job_id} did not create attempt for window {window_index}")


async def wait_for_jobs_running(database: Database, expected: int) -> None:
    for _ in range(500):
        jobs = await database.list_jobs(limit=100)
        if sum(item["status"] == "running" for item in jobs) == expected:
            return
        await asyncio.sleep(0.002)
    raise AssertionError(f"Expected {expected} running jobs")


@pytest.mark.asyncio
async def test_submission_gate_prefers_higher_job_completion() -> None:
    gate = _ISliceSubmissionGate()
    url = "http://islice.test"
    await gate.acquire(url, "holder", 0)
    low = asyncio.create_task(gate.acquire(url, "low", 25))
    await asyncio.sleep(0)
    high = asyncio.create_task(gate.acquire(url, "high", 50))
    await asyncio.sleep(0)

    await gate.release(url, "holder")
    await asyncio.wait_for(high, timeout=1)
    assert not low.done()
    await gate.release(url, "high")
    await asyncio.wait_for(low, timeout=1)
    await gate.release(url, "low")


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
    assert [item["content_type"] for item in segments] == ["新闻", "广告", "广告", "电视剧"]
    assert all(item["submitted_at"] for item in await database.get_attempts_for_job("abc123"))
    manifest = json.loads(
        (configured.data_dir / "jobs" / "abc123" / "result.json").read_text(encoding="utf-8")
    )
    assert manifest["schemaVersion"] == 5
    assert manifest["job"]["status"] == "completed"
    assert len(manifest["segments"]) == 4
    assert manifest["segments"][0]["content_type"] == "新闻"
    assert manifest["segments"][0]["ignored"] is False
    assert not list(configured.temp_dir.rglob("*.ts"))


@pytest.mark.asyncio
async def test_jobs_take_turns_at_71_percent_with_highest_completion_first(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.ts"
    source.write_bytes(b"source")
    configured = replace(make_settings(tmp_path), max_active_jobs=3)
    database = Database(configured.database_path)
    await database.initialize()
    await create_named_job(database, source, "job-a", 7200)
    await create_named_job(database, source, "job-b", 14400)
    await create_named_job(database, source, "job-c", 7200)
    islice = ControlledISlice()
    orchestrator = Orchestrator(configured, database, FakeMedia(), islice)

    tasks: list[asyncio.Task[None]] = []
    try:
        tasks.append(asyncio.create_task(orchestrator._process_job("job-a")))
        a0 = await asyncio.wait_for(islice.created.get(), timeout=1)
        assert a0 == "sh-job-a-w000-a1"
        islice.set_state(a0, "processing", 71)

        tasks.append(asyncio.create_task(orchestrator._process_job("job-b")))
        b0 = await asyncio.wait_for(islice.created.get(), timeout=1)
        assert b0 == "sh-job-b-w000-a1"
        islice.set_state(b0, "processing", 71)

        tasks.append(asyncio.create_task(orchestrator._process_job("job-c")))
        c0 = await asyncio.wait_for(islice.created.get(), timeout=1)
        assert c0 == "sh-job-c-w000-a1"

        # A and B finish, but C still owns the pre-71 submission turn.
        islice.set_state(a0, "completed", 100)
        islice.set_state(b0, "completed", 100)
        await wait_for_current_window(database, "job-a", 1)
        await wait_for_current_window(database, "job-b", 1)
        await wait_for_attempt(database, "job-a", 1)
        await wait_for_attempt(database, "job-b", 1)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(islice.created.get(), timeout=0.05)

        # A is 50% complete and B is 25% complete, so A wins even if both wait.
        islice.set_state(c0, "processing", 71)
        a1 = await asyncio.wait_for(islice.created.get(), timeout=1)
        assert a1 == "sh-job-a-w001-a1"

        # B cannot create its next task until A's latest task reaches 71%.
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(islice.created.get(), timeout=0.05)
        islice.set_state(a1, "processing", 71)
        b1 = await asyncio.wait_for(islice.created.get(), timeout=1)
        assert b1 == "sh-job-b-w001-a1"
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_multiple_islices_enforce_capacity_and_priority_independently(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.ts"
    source.write_bytes(b"source")
    urls = (
        "http://islice-a.test",
        "http://islice-b.test",
        "http://islice-c.test",
    )
    configured = replace(
        make_settings(tmp_path, duration_timeout=60),
        islice_base_urls=urls,
        max_active_jobs=21,
    )
    database = Database(configured.database_path)
    await database.initialize()
    stat = source.stat()
    job_ids = [f"multi-job-{index:02d}" for index in range(21)]
    for job_id in job_ids:
        await database.create_job(
            {
                "id": job_id,
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

    clients = {url: CapacityISlice(capacity=5) for url in urls}
    pool = ISlicePool(configured)
    await pool.close()
    pool.clients = clients
    orchestrator = Orchestrator(configured, database, FakeMedia(), pool)
    await orchestrator.start()
    try:
        await wait_for_jobs_running(database, 21)
        jobs = await database.list_jobs(limit=100)
        assigned = {
            url: {item["id"] for item in jobs if item["islice_base_url"] == url}
            for url in urls
        }
        assert {url: len(ids) for url, ids in assigned.items()} == {
            urls[0]: 7,
            urls[1]: 7,
            urls[2]: 7,
        }

        first_five: dict[str, list[str]] = {url: [] for url in urls}
        for _round in range(5):
            for url in urls:
                task_id = await asyncio.wait_for(
                    clients[url].created.get(), timeout=10
                )
                assert clients[url].states[task_id] == ("processing", 0.0)
                first_five[url].append(task_id)
            for url in urls:
                clients[url].set_progress(first_five[url][-1], 71)

        sixth: dict[str, str] = {}
        for url in urls:
            sixth[url] = await asyncio.wait_for(clients[url].created.get(), timeout=10)
            assert clients[url].states[sixth[url]] == ("pending", 0.0)
            assert clients[url].processing_count == 5
            assert clients[url].max_processing == 5

        # A pending sixth task owns the submission turn, so neither instance may
        # create a seventh remote task yet.
        for url in urls:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(clients[url].created.get(), timeout=0.05)

        completed_job: dict[str, str] = {}
        for url in urls:
            first_task = first_five[url][0]
            completed_job[url] = first_task[3:].rsplit("-w", 1)[0]
            assert completed_job[url] in assigned[url]
            clients[url].complete(first_task)
            assert clients[url].states[sixth[url]] == ("processing", 0.0)
        for url in urls:
            await wait_for_attempt(database, completed_job[url], 1)

        # Releasing one instance must not affect the other instance's gate.
        clients[urls[0]].set_progress(sixth[urls[0]], 71)
        left_next = await asyncio.wait_for(clients[urls[0]].created.get(), timeout=10)
        assert left_next == f"sh-{completed_job[urls[0]]}-w001-a1"
        assert clients[urls[0]].states[left_next] == ("pending", 0.0)
        assert clients[urls[1]].created.empty()
        assert clients[urls[2]].created.empty()

        for url in urls[1:]:
            clients[url].set_progress(sixth[url], 71)
            next_task = await asyncio.wait_for(clients[url].created.get(), timeout=10)
            assert next_task == f"sh-{completed_job[url]}-w001-a1"
            assert clients[url].states[next_task] == ("pending", 0.0)

        # The 50%-complete jobs won over each instance's untouched seventh job,
        # and simulated processing never exceeded five tasks per iSlice.
        for url in urls:
            assert clients[url].max_processing == 5
            assert clients[url].processing_count == 5
            assert len(clients[url].creation_order) == 7
    finally:
        await orchestrator.stop()
        await pool.close()


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
async def test_tail_rebuild_keeps_old_tasks_and_uses_new_generation(tmp_path: Path) -> None:
    source = tmp_path / "source.ts"
    source.write_bytes(b"source")
    configured = make_settings(tmp_path)
    database = Database(configured.database_path)
    await database.initialize()
    await create_job(database, source, duration=7200)
    orchestrator = Orchestrator(configured, database, FakeMedia(), FakeISlice())
    await orchestrator._process_job("abc123")
    await database.update_job(
        "abc123", islice_base_url="http://islice.test", reviewed=1
    )
    old_windows = await database.get_windows("abc123")
    old_attempt = (await database.get_attempts(old_windows[1]["id"]))[0]
    old_task = old_attempt["task_id"]
    old_raw_path = Path(old_attempt["raw_response_path"])
    old_chunk_path = configured.temp_dir / "abc123" / "retained-old-window.ts"
    old_chunk_path.write_bytes(b"retained")
    await database.update_window(
        old_windows[1]["id"], chunk_path=str(old_chunk_path)
    )

    replacement = ResplitISlice()
    orchestrator.islice = replacement
    preview = await orchestrator.preview_tail_rebuild("abc123", 1)
    response = await orchestrator.start_tail_rebuild(
        "abc123", 1, preview["previewToken"], "从窗口 2 重跑"
    )

    assert response["generation"] == 1
    assert replacement.deleted == []
    assert old_raw_path.is_file()
    assert old_chunk_path.is_file()
    assert [item["window_index"] for item in await database.get_windows("abc123")] == [0]
    truncated = await database.get_job("abc123")
    assert truncated["status"] == "pending_schedule"
    assert truncated["current_window"] == 1
    assert truncated["reviewed"] == 0
    assert truncated["rebuild_revision"] == 1
    rebuild = await database.get_latest_job_rebuild("abc123")
    assert Path(rebuild["snapshot_path"]).is_file()
    snapshot = json.loads(Path(rebuild["snapshot_path"]).read_text(encoding="utf-8"))
    assert old_task in {attempt["task_id"] for attempt in snapshot["attempts"]}

    await database.update_job("abc123", status="queued")
    await orchestrator._process_job("abc123")
    created_task, request = replacement.created[-1]
    assert created_task == "sh-abc123-w001-g1-a1"
    assert request["videoPath"].endswith("/internal/chunks/abc123/1/1.ts")
    assert (await database.get_job("abc123"))["status"] == "completed"
    assert (await database.get_latest_job_rebuild("abc123"))["status"] == "completed"
@pytest.mark.asyncio
async def test_tail_rebuild_snapshot_failure_does_not_change_database(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "source.ts"
    source.write_bytes(b"source")
    configured = make_settings(tmp_path)
    database = Database(configured.database_path)
    await database.initialize()
    await create_job(database, source, duration=3600)
    orchestrator = Orchestrator(configured, database, FakeMedia(), FakeISlice())
    await orchestrator._process_job("abc123")
    await database.update_job("abc123", islice_base_url="http://islice.test")
    preview = await orchestrator.preview_tail_rebuild("abc123", 0)
    before_windows = await database.get_windows("abc123")

    def fail_snapshot(_path, _payload):
        raise OSError("disk full")

    monkeypatch.setattr(orchestrator, "_atomic_json", fail_snapshot)
    with pytest.raises(Exception, match="Could not save the rebuild snapshot"):
        await orchestrator.start_tail_rebuild(
            "abc123", 0, preview["previewToken"], "从窗口 1 重跑"
        )
    assert await database.get_windows("abc123") == before_windows
    assert (await database.get_job("abc123"))["rebuild_revision"] == 0
    assert await database.get_latest_job_rebuild("abc123") is None


@pytest.mark.asyncio
async def test_manual_resplit_reuses_task_id_and_replaces_window_results(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.ts"
    source.write_bytes(b"source")
    configured = make_settings(tmp_path)
    database = Database(configured.database_path)
    await database.initialize()
    await create_job(database, source, duration=3600)
    orchestrator = Orchestrator(configured, database, FakeMedia(), FakeISlice())
    await orchestrator._process_job("abc123")
    await database.update_job("abc123", islice_base_url="http://islice.test")

    window = (await database.get_windows("abc123"))[0]
    attempts_before = await database.get_attempts(window["id"])
    assert len(attempts_before) == 1
    task_id = attempts_before[0]["task_id"]
    first_submitted_at = attempts_before[0]["submitted_at"]
    assert first_submitted_at
    assert not Path(window["chunk_path"]).exists()
    assert {item["title"] for item in await database.get_segments("abc123")} == {
        "first",
        "handoff",
    }

    replacement = ResplitISlice()
    orchestrator.islice = replacement
    response = await orchestrator.schedule_resplit("abc123", 0, task_id)
    task = orchestrator._resplit_tasks[("abc123", 0)]
    await asyncio.wait_for(task, timeout=1)

    assert response["taskId"] == task_id
    assert replacement.deleted == [task_id]
    assert [item[0] for item in replacement.created] == [task_id]
    assert replacement.created[0][1]["taskId"] == task_id
    attempts_after = await database.get_attempts(window["id"])
    assert len(attempts_after) == 1
    assert attempts_after[0]["task_id"] == task_id
    assert attempts_after[0]["status"] == "completed"
    assert attempts_after[0]["submitted_at"] > first_submitted_at
    segments = await database.get_segments("abc123")
    assert [item["title"] for item in segments] == ["replacement"]
    assert (await database.get_job("abc123"))["status"] == "completed"
    assert not Path(window["chunk_path"]).exists()


@pytest.mark.asyncio
async def test_manual_resplit_overlap_can_be_explicitly_accepted_without_islice_call(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.ts"
    source.write_bytes(b"source")
    configured = make_settings(tmp_path)
    database = Database(configured.database_path)
    await database.initialize()
    await create_job(database, source, duration=7200)
    orchestrator = Orchestrator(configured, database, FakeMedia(), FakeISlice())
    await orchestrator._process_job("abc123")
    await database.update_job("abc123", islice_base_url="http://islice.test")

    window = (await database.get_windows("abc123"))[0]
    task_id = (await database.get_attempts(window["id"]))[0]["task_id"]
    replacement = ResplitISlice()
    orchestrator.islice = replacement
    await orchestrator.schedule_resplit("abc123", 0, task_id)
    await asyncio.wait_for(orchestrator._resplit_tasks[("abc123", 0)], timeout=1)

    paused = await database.get_job("abc123")
    failed_window = await database.get_window("abc123", 0)
    attempt = (await database.get_attempts(window["id"]))[0]
    assert paused["status"] == "paused"
    assert failed_window["status"] == "failed"
    assert "resplit handoff changed" in failed_window["error_message"]
    assert attempt["status"] == "completed"
    assert attempt["service_status"] == "completed"
    assert Path(attempt["raw_response_path"]).is_file()
    assert [item["title"] for item in await database.get_segments("abc123")][:2] == [
        "first",
        "handoff",
    ]

    response = await orchestrator.accept_resplit_overlap("abc123", 0, task_id)

    assert response["status"] == "overlap_accepted"
    assert replacement.deleted == [task_id]
    assert len(replacement.created) == 1
    job = await database.get_job("abc123")
    windows = await database.get_windows("abc123")
    segments = await database.get_segments("abc123")
    assert job["status"] == "completed"
    assert all(item["status"] == "completed" for item in windows)
    assert [item["title"] for item in segments] == [
        "replacement",
        "handoff-redone",
        "last",
    ]
    assert float(segments[0]["global_end"]) == 3600
    assert float(segments[1]["global_start"]) == 3500
    assert any(
        "accepted with cross-window time overlap" in warning
        for warning in json.loads(job["warnings_json"])
    )
    assert not Path(window["chunk_path"]).exists()


@pytest.mark.asyncio
async def test_manual_resplit_rejects_a_second_request_for_the_same_job(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.ts"
    source.write_bytes(b"source")
    configured = make_settings(tmp_path)
    database = Database(configured.database_path)
    await database.initialize()
    await create_job(database, source, duration=3600)
    orchestrator = Orchestrator(configured, database, FakeMedia(), FakeISlice())
    await orchestrator._process_job("abc123")
    await database.update_job("abc123", islice_base_url="http://islice.test")
    window = (await database.get_windows("abc123"))[0]
    task_id = (await database.get_attempts(window["id"]))[0]["task_id"]

    replacement = BlockingResplitISlice()
    orchestrator.islice = replacement
    await orchestrator.schedule_resplit("abc123", 0, task_id)
    task = orchestrator._resplit_tasks[("abc123", 0)]
    await asyncio.wait_for(replacement.delete_started.wait(), timeout=1)
    with pytest.raises(ResplitConflictError, match="already has a resplit"):
        await orchestrator.schedule_resplit("abc123", 0, task_id)
    replacement.allow_delete.set()
    await asyncio.wait_for(task, timeout=1)


@pytest.mark.asyncio
async def test_manual_resplit_failure_pauses_without_creating_an_attempt(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.ts"
    source.write_bytes(b"source")
    configured = make_settings(tmp_path)
    database = Database(configured.database_path)
    await database.initialize()
    await create_job(database, source, duration=3600)
    orchestrator = Orchestrator(configured, database, FakeMedia(), FakeISlice())
    await orchestrator._process_job("abc123")
    await database.update_job("abc123", islice_base_url="http://islice.test")
    window = (await database.get_windows("abc123"))[0]
    task_id = (await database.get_attempts(window["id"]))[0]["task_id"]

    orchestrator.islice = ResplitISlice(terminal_status="failed")
    await orchestrator.schedule_resplit("abc123", 0, task_id)
    task = orchestrator._resplit_tasks[("abc123", 0)]
    await asyncio.wait_for(task, timeout=1)

    job = await database.get_job("abc123")
    attempts = await database.get_attempts(window["id"])
    assert job["status"] == "paused"
    assert "manual resplit failed" in job["error_message"]
    assert len(attempts) == 1
    assert attempts[0]["task_id"] == task_id
    assert attempts[0]["status"] == "failed"
    assert Path(window["chunk_path"]).is_file()


@pytest.mark.asyncio
async def test_submission_failure_pauses_without_automatic_retry(tmp_path: Path) -> None:
    source = tmp_path / "source.ts"
    source.write_bytes(b"source")
    configured = make_settings(tmp_path)
    database = Database(configured.database_path)
    await database.initialize()
    await create_job(database, source, duration=3600)
    islice = FailingISlice()
    orchestrator = Orchestrator(configured, database, FakeMedia(), islice)

    await orchestrator._process_job("abc123")

    job = await database.get_job("abc123")
    window = (await database.get_windows("abc123"))[0]
    assert job["status"] == "paused"
    assert "service unavailable" in job["error_message"]
    assert islice.calls == 1
    assert len(await database.get_attempts(window["id"])) == 1
    assert Path(window["chunk_path"]).is_file()

    await database.update_job("abc123", status="queued", pause_requested=0)
    await orchestrator._process_job("abc123")

    assert (await database.get_job("abc123"))["status"] == "paused"
    assert islice.calls == 2
    assert len(await database.get_attempts(window["id"])) == 2


@pytest.mark.asyncio
async def test_terminal_islice_failure_pauses_without_automatic_retry(tmp_path: Path) -> None:
    source = tmp_path / "source.ts"
    source.write_bytes(b"source")
    configured = make_settings(tmp_path)
    database = Database(configured.database_path)
    await database.initialize()
    await create_job(database, source, duration=3600)
    islice = TerminalFailingISlice()
    orchestrator = Orchestrator(configured, database, FakeMedia(), islice)

    await orchestrator._process_job("abc123")

    job = await database.get_job("abc123")
    window = (await database.get_windows("abc123"))[0]
    attempts = await database.get_attempts(window["id"])
    assert job["status"] == "paused"
    assert "internal retries exhausted" in job["error_message"]
    assert window["status"] == "failed"
    assert window["error_message"] == "internal retries exhausted"
    assert islice.calls == 1
    assert len(attempts) == 1
    assert attempts[0]["status"] == "failed"
    assert attempts[0]["service_status"] == "failed"
    assert attempts[0]["error_message"] == "internal retries exhausted"
    assert attempts[0]["raw_response_path"]


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
