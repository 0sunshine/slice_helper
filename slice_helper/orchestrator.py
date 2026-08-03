from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .config import Settings
from .database import Database, utc_now
from .islice import ISliceClient, ISliceError
from .media import MediaError, MediaService
from .models import CutMode, JobStatus, WindowStatus
from .processing import (
    SegmentValidationError,
    calculate_window_end,
    process_segments,
)


logger = logging.getLogger(__name__)


class JobControlRequested(Exception):
    """The current job reached a safe point for a pause or stop request."""


class Orchestrator:
    RETRY_DELAYS = (5.0, 15.0, 45.0)

    def __init__(
        self,
        settings: Settings,
        database: Database,
        media: MediaService,
        islice: ISliceClient,
    ):
        self.settings = settings
        self.database = database
        self.media = media
        self.islice = islice
        self._scheduler_task: asyncio.Task[None] | None = None
        self._active: dict[str, asyncio.Task[None]] = {}
        self._wake = asyncio.Event()
        self._stopping = False

    async def start(self) -> None:
        await self.database.recover_jobs()
        self._stopping = False
        self._scheduler_task = asyncio.create_task(self._scheduler_loop(), name="job-scheduler")

    async def stop(self) -> None:
        self._stopping = True
        self._wake.set()
        if self._scheduler_task:
            self._scheduler_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._scheduler_task
        for task in self._active.values():
            task.cancel()
        if self._active:
            await asyncio.gather(*self._active.values(), return_exceptions=True)
        self._active.clear()

    def notify(self) -> None:
        self._wake.set()

    async def _scheduler_loop(self) -> None:
        while not self._stopping:
            capacity = self.settings.max_active_jobs - len(self._active)
            if capacity > 0:
                for job in await self.database.get_runnable_jobs(capacity):
                    job_id = job["id"]
                    if job_id in self._active:
                        continue
                    task = asyncio.create_task(self._process_job(job_id), name=f"job-{job_id}")
                    self._active[job_id] = task
                    task.add_done_callback(lambda _task, key=job_id: self._active.pop(key, None))
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=1.0)
            except TimeoutError:
                pass

    async def _process_job(self, job_id: str) -> None:
        try:
            job = await self.database.get_job(job_id)
            if not job or job["status"] != JobStatus.QUEUED.value:
                return
            fields: dict[str, Any] = {"status": JobStatus.RUNNING.value, "error_message": ""}
            if not job.get("started_at"):
                fields["started_at"] = utc_now()
            await self.database.update_job(job_id, **fields)
            await self._cleanup_completed_chunks(job_id)

            while True:
                job = await self.database.get_job(job_id)
                if not job:
                    return
                if await self._honor_control_request(job):
                    return
                if not self._source_unchanged(job):
                    await self._pause_job(job_id, "Source file size or modification time changed")
                    return

                index = int(job["current_window"])
                total_windows = int(job["total_windows"])
                if index >= total_windows:
                    await self.database.update_job(
                        job_id,
                        status=JobStatus.COMPLETED.value,
                        progress=100.0,
                        completed_at=utc_now(),
                    )
                    await self.write_manifest(job_id)
                    return

                nominal_end = calculate_window_end(
                    index,
                    total_windows,
                    float(job["source_duration"]),
                    self.settings.window_seconds,
                )
                requested_start = float(job["next_window_start"] if index else 0.0)
                window = await self.database.upsert_window(job_id, index, requested_start, nominal_end)
                if window["status"] == WindowStatus.COMPLETED.value:
                    next_start = (
                        window["handoff_start"]
                        if window["handoff_start"] is not None
                        else window["nominal_end"]
                    )
                    await self._advance_job(job_id, index, total_windows, float(next_start))
                    await self._remove_chunk(job_id, window)
                    continue

                completed = await self._process_window(job, window)
                if not completed:
                    return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Job %s paused after an unexpected error", job_id)
            await self._pause_job(job_id, str(exc))

    async def _honor_control_request(self, job: dict[str, Any]) -> bool:
        if job["status"] == JobStatus.STOP_REQUESTED.value or job["stop_requested"]:
            await self.database.update_job(
                job["id"], status=JobStatus.STOPPED.value, stop_requested=0
            )
            return True
        if job["status"] == JobStatus.PAUSE_REQUESTED.value or job["pause_requested"]:
            await self.database.update_job(
                job["id"], status=JobStatus.PAUSED.value, pause_requested=0
            )
            return True
        return False

    @staticmethod
    def _source_unchanged(job: dict[str, Any]) -> bool:
        try:
            stat = Path(job["source_path"]).stat()
        except OSError:
            return False
        return stat.st_size == job["source_size"] and stat.st_mtime_ns == job["source_mtime_ns"]

    async def _process_window(self, job: dict[str, Any], window: dict[str, Any]) -> bool:
        job_id = job["id"]
        index = int(window["window_index"])
        start = float(window["requested_start"])
        end = float(window["nominal_end"])
        job_temp_dir = self.settings.temp_dir / job_id
        chunk_path = job_temp_dir / f"window-{index:03d}.ts"
        chunk_url = (
            f"{self.settings.public_base_url}/internal/chunks/{job_id}/{index}.ts"
        )

        if not chunk_path.is_file():
            await self.database.update_window(window["id"], status=WindowStatus.CUTTING.value)
            await self.media.cut(
                Path(job["source_path"]),
                chunk_path,
                start,
                end,
                CutMode(job["cut_mode"]),
            )
        await self.database.update_window(
            window["id"],
            status=WindowStatus.READY.value,
            chunk_path=str(chunk_path),
            chunk_url=chunk_url,
            error_message="",
        )
        window = await self.database.get_window(job_id, index) or window
        if await self._control_requested(job_id):
            return False

        attempt_items = await self.database.get_attempts(window["id"])
        attempts = {item["attempt_no"]: item for item in attempt_items}
        terminal_payload: dict[str, Any] | None = None
        terminal_attempt: dict[str, Any] | None = None
        for attempt in attempt_items:
            if attempt["status"] == "completed" and attempt["raw_response_path"]:
                path = Path(attempt["raw_response_path"])
                if path.is_file():
                    try:
                        terminal_payload = json.loads(path.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError):
                        terminal_payload = None
                    else:
                        terminal_attempt = attempt
                        break

        if terminal_payload is None:
            reusable = next(
                (attempt for attempt in attempt_items if attempt["status"] != "failed"),
                None,
            )
            first_attempt_no = (
                int(reusable["attempt_no"])
                if reusable
                else max(attempts, default=0) + 1
            )
            run_attempt_numbers = range(
                first_attempt_no,
                first_attempt_no + self.settings.max_service_attempts,
            )
            for run_index, attempt_no in enumerate(run_attempt_numbers):
                if await self._control_requested(job_id):
                    return False
                attempt = attempts.get(attempt_no)
                if not attempt:
                    task_id = f"sh-{job_id[:16]}-w{index:03d}-a{attempt_no}"
                    attempt = await self.database.create_attempt(
                        window["id"], attempt_no, task_id
                    )
                    attempts[attempt_no] = attempt
                if attempt["status"] == "failed":
                    continue

                request = self._create_task_request(
                    job, attempt["task_id"], chunk_url, start
                )
                try:
                    existing = await self.islice.ensure_task(attempt["task_id"], request)
                    await self._raise_if_control_requested(job_id)
                    await self.database.update_attempt(
                        attempt["id"],
                        status="polling",
                        error_message="",
                    )
                    await self.database.update_window(
                        window["id"], status=WindowStatus.POLLING.value
                    )
                    payload = (
                        existing
                        if self._is_terminal(existing)
                        else await self._poll(job_id, attempt["task_id"])
                    )
                    raw_path = await self._write_raw(job_id, index, attempt_no, payload)
                    status = str(payload["taskInfo"].get("status") or "")
                    if status == "completed":
                        await self.database.update_attempt(
                            attempt["id"],
                            status="completed",
                            raw_response_path=str(raw_path),
                            finished_at=utc_now(),
                        )
                        terminal_payload = payload
                        terminal_attempt = attempt
                        break
                    error = str(
                        payload["taskInfo"].get("errorMessage")
                        or "iSlice task failed"
                    )
                    await self.database.update_attempt(
                        attempt["id"],
                        status="failed",
                        raw_response_path=str(raw_path),
                        error_message=error,
                        finished_at=utc_now(),
                    )
                except JobControlRequested:
                    return False
                except (ISliceError, TimeoutError) as exc:
                    await self.database.update_attempt(
                        attempt["id"],
                        status="failed",
                        error_message=str(exc),
                        finished_at=utc_now(),
                    )
                if run_index < self.settings.max_service_attempts - 1:
                    try:
                        await self._sleep_with_control(
                            job_id, self.RETRY_DELAYS[run_index]
                        )
                    except JobControlRequested:
                        return False

        if terminal_payload is None or terminal_attempt is None:
            attempt_count = self.settings.max_service_attempts
            await self.database.update_window(
                window["id"],
                status=WindowStatus.FAILED.value,
                error_message=f"iSlice failed after {attempt_count} attempts",
            )
            await self._pause_job(
                job_id, f"Window {index + 1} failed after {attempt_count} attempts"
            )
            return False

        is_final = index == int(job["total_windows"]) - 1
        base_time = datetime.fromisoformat(job["program_start_time"]) if job["program_start_time"] else None
        try:
            processed = process_segments(
                terminal_payload.get("segments"),
                window_start=start,
                chunk_duration=end - start,
                is_final_window=is_final,
                handoff_max_seconds=self.settings.handoff_max_seconds,
                program_start_time=base_time,
            )
            await self._validate_previous_boundary(job_id, index, processed.segments)
        except SegmentValidationError as exc:
            await self.database.update_window(
                window["id"], status=WindowStatus.FAILED.value, error_message=str(exc)
            )
            await self._pause_job(job_id, f"Window {index + 1}: {exc}")
            return False

        await self.database.replace_window_segments(job_id, window["id"], processed.segments)
        if processed.warning:
            await self.database.append_warning(job_id, f"Window {index + 1}: {processed.warning}")
        next_start = processed.next_window_start if processed.next_window_start is not None else end
        await self.database.update_window(
            window["id"],
            status=WindowStatus.COMPLETED.value,
            handoff_start=processed.handoff_start,
            error_message="",
        )
        await self._advance_job(job_id, index, int(job["total_windows"]), float(next_start))
        await self.write_manifest(job_id)
        await self._remove_chunk(job_id, {**window, "chunk_path": str(chunk_path)})

        current = await self.database.get_job(job_id)
        if current and await self._honor_control_request(current):
            return False
        return True

    async def _advance_job(
        self, job_id: str, index: int, total_windows: int, next_start: float
    ) -> None:
        completed = index + 1
        await self.database.update_job(
            job_id,
            current_window=completed,
            next_window_start=next_start,
            progress=min(100.0, completed / total_windows * 100.0),
        )

    def _create_task_request(
        self, job: dict[str, Any], task_id: str, chunk_url: str, window_start: float
    ) -> dict[str, Any]:
        request: dict[str, Any] = {
            "taskId": task_id,
            "videoPath": chunk_url,
            "templateId": job["template_id"],
            "language": job["language"],
        }
        if job["channel_name"]:
            request["channelName"] = job["channel_name"]
        if job["program_start_time"]:
            base = datetime.fromisoformat(job["program_start_time"])
            request["programStartTime"] = (base + timedelta(seconds=window_start)).isoformat()
        return request

    async def _poll(self, job_id: str, task_id: str) -> dict[str, Any]:
        deadline = time.monotonic() + self.settings.window_timeout_seconds
        consecutive_errors = 0
        while time.monotonic() < deadline:
            await self._raise_if_control_requested(job_id)
            try:
                payload = await self.islice.get_task_info(task_id)
                if payload is None:
                    raise ISliceError("Task disappeared from iSlice")
                consecutive_errors = 0
            except ISliceError:
                consecutive_errors += 1
                if consecutive_errors >= 3:
                    raise
                await self._sleep_with_control(
                    job_id, self.RETRY_DELAYS[consecutive_errors - 1]
                )
                continue
            await self._raise_if_control_requested(job_id)
            if self._is_terminal(payload):
                return payload
            await self._sleep_with_control(job_id, self.settings.poll_interval_seconds)
        raise TimeoutError(f"Task {task_id} exceeded the window timeout")

    async def _control_requested(self, job_id: str) -> bool:
        job = await self.database.get_job(job_id)
        return bool(job and await self._honor_control_request(job))

    async def _raise_if_control_requested(self, job_id: str) -> None:
        if await self._control_requested(job_id):
            raise JobControlRequested

    async def _sleep_with_control(self, job_id: str, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while True:
            await self._raise_if_control_requested(job_id)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            await asyncio.sleep(min(1.0, remaining))

    async def _validate_previous_boundary(
        self, job_id: str, window_index: int, segments: list[dict[str, Any]]
    ) -> None:
        accepted = [segment for segment in segments if segment["accepted"]]
        if not accepted:
            return
        existing = await self.database.get_segments(job_id, accepted_only=True)
        previous = [
            segment for segment in existing if int(segment["window_index"]) < window_index
        ]
        if not previous:
            return
        previous_end = max(float(segment["global_end"]) for segment in previous)
        current_start = min(float(segment["global_start"]) for segment in accepted)
        if current_start < previous_end - 1.0:
            raise SegmentValidationError(
                "accepted segments overlap the previous window: "
                f"previous end {previous_end:.3f}, current start {current_start:.3f}"
            )

    async def _cleanup_completed_chunks(self, job_id: str) -> None:
        for window in await self.database.get_windows(job_id):
            if window["status"] == WindowStatus.COMPLETED.value:
                await self._remove_chunk(job_id, window)

    async def _remove_chunk(self, job_id: str, window: dict[str, Any]) -> None:
        raw_path = str(window.get("chunk_path") or "")
        if not raw_path:
            return
        try:
            Path(raw_path).unlink(missing_ok=True)
        except OSError as exc:
            warning = (
                f"Could not delete completed window {int(window['window_index']) + 1} "
                f"chunk: {exc}"
            )
            logger.warning("Job %s: %s", job_id, warning)
            await self.database.append_warning(job_id, warning)

    @staticmethod
    def _is_terminal(payload: dict[str, Any] | None) -> bool:
        if not payload:
            return False
        return str(payload.get("taskInfo", {}).get("status") or "") in {"completed", "failed"}

    async def _write_raw(
        self, job_id: str, window_index: int, attempt_no: int, payload: dict[str, Any]
    ) -> Path:
        path = (
            self.settings.data_dir
            / "jobs"
            / job_id
            / "raw"
            / f"window-{window_index:03d}-attempt-{attempt_no}.json"
        )
        await asyncio.to_thread(self._atomic_json, path, payload)
        return path

    async def write_manifest(self, job_id: str) -> dict[str, Any]:
        job = await self.database.get_job(job_id)
        if not job:
            raise KeyError(job_id)
        windows = await self.database.get_windows(job_id)
        attempts = await self.database.get_attempts_for_job(job_id)
        segments = await self.database.get_segments(job_id)
        for segment in segments:
            segment["accepted"] = bool(segment["accepted"])
            segment["keywords"] = json.loads(segment.pop("keywords_json"))
            segment["raw"] = json.loads(segment.pop("raw_json"))
        manifest = {
            "schemaVersion": 1,
            "generatedAt": utc_now(),
            "externalMediaMayExpire": True,
            "job": {
                **self._manifest_job(job),
            },
            "windows": windows,
            "attempts": attempts,
            "segments": segments,
        }
        path = self.settings.data_dir / "jobs" / job_id / "result.json"
        await asyncio.to_thread(self._atomic_json, path, manifest)
        return manifest

    @staticmethod
    def _manifest_job(job: dict[str, Any]) -> dict[str, Any]:
        payload = dict(job)
        payload["pause_requested"] = bool(payload["pause_requested"])
        payload["stop_requested"] = bool(payload["stop_requested"])
        payload["warnings"] = json.loads(payload.pop("warnings_json") or "[]")
        return payload

    @staticmethod
    def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)

    async def _pause_job(self, job_id: str, message: str) -> None:
        await self.database.update_job(
            job_id,
            status=JobStatus.PAUSED.value,
            pause_requested=0,
            error_message=message,
        )
        with contextlib.suppress(Exception):
            await self.write_manifest(job_id)
