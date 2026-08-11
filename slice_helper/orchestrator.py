from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import os
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .config import Settings
from .database import Database, utc_now
from .islice import ISliceClient, ISliceError, ISlicePool
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


class ResplitConflictError(RuntimeError):
    pass


class ResplitValidationError(RuntimeError):
    pass


class RebuildConflictError(RuntimeError):
    pass


class RebuildValidationError(RuntimeError):
    pass


class _ISliceSubmissionGate:
    """Progress-priority admission gate for pre-LLM work on each iSlice."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._holders: dict[str, str] = {}
        self._waiters: dict[
            str, list[tuple[str, float, int, asyncio.Future[None]]]
        ] = defaultdict(list)
        self._sequence = 0

    async def acquire(self, base_url: str, ticket: str, priority: float) -> None:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[None] = loop.create_future()
        async with self._lock:
            if self._holders.get(base_url) == ticket:
                return
            self._sequence += 1
            self._waiters[base_url].append(
                (ticket, priority, self._sequence, future)
            )
            self._grant_next(base_url)
        try:
            await future
        except asyncio.CancelledError:
            async with self._lock:
                if self._holders.get(base_url) == ticket:
                    self._holders.pop(base_url, None)
                self._grant_next(base_url)
            raise

    async def release(self, base_url: str, ticket: str) -> bool:
        async with self._lock:
            if self._holders.get(base_url) != ticket:
                return False
            self._holders.pop(base_url, None)
            self._grant_next(base_url)
            return True

    def _grant_next(self, base_url: str) -> None:
        if base_url in self._holders:
            return
        queue = self._waiters[base_url]
        queue[:] = [item for item in queue if not item[3].cancelled()]
        if queue:
            best_index = max(
                range(len(queue)),
                key=lambda index: (queue[index][1], -queue[index][2]),
            )
            ticket, _priority, _sequence, future = queue.pop(best_index)
            self._holders[base_url] = ticket
            future.set_result(None)
            return
        self._waiters.pop(base_url, None)


class Orchestrator:
    RETRY_DELAYS = (5.0, 15.0, 45.0)

    def __init__(
        self,
        settings: Settings,
        database: Database,
        media: MediaService,
        islice: ISliceClient | ISlicePool,
    ):
        self.settings = settings
        self.database = database
        self.media = media
        self.islice = islice
        self._scheduler_task: asyncio.Task[None] | None = None
        self._active: dict[str, asyncio.Task[None]] = {}
        self._submission_gate = _ISliceSubmissionGate()
        self._gate_monitors: dict[str, asyncio.Task[None]] = {}
        self._resplit_tasks: dict[tuple[str, int], asyncio.Task[None]] = {}
        self._resplit_lock = asyncio.Lock()
        self._rebuild_lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._stopping = False

    async def start(self) -> None:
        await self.database.recover_interrupted_resplits()
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
        for task in self._resplit_tasks.values():
            task.cancel()
        if self._resplit_tasks:
            await asyncio.gather(*self._resplit_tasks.values(), return_exceptions=True)
        self._resplit_tasks.clear()
        for task in self._gate_monitors.values():
            task.cancel()
        if self._gate_monitors:
            await asyncio.gather(*self._gate_monitors.values(), return_exceptions=True)
        self._gate_monitors.clear()

    def notify(self) -> None:
        self._wake.set()

    async def preview_tail_rebuild(
        self, job_id: str, start_window_index: int
    ) -> dict[str, Any]:
        async with self._rebuild_lock:
            job = await self.database.get_job(job_id)
            self._validate_tail_rebuild_job(job_id, job)
            try:
                state = await self.database.get_rebuild_preview(
                    job_id, start_window_index
                )
            except ValueError as exc:
                raise RebuildValidationError(str(exc)) from exc
            if state is None:
                raise RebuildValidationError("Job not found")
            previous = state["previous_window"]
            if start_window_index > 0 and (
                previous is None or previous["status"] != WindowStatus.COMPLETED.value
            ):
                raise RebuildValidationError(
                    "The window before the rebuild point is not completed"
                )
            start = float(
                previous["handoff_start"]
                if previous and previous["handoff_start"] is not None
                else previous["nominal_end"] if previous else 0.0
            )
            base_time = (
                datetime.fromisoformat(job["program_start_time"])
                if job and job.get("program_start_time")
                else None
            )
            return {
                "jobId": job_id,
                "startWindowIndex": start_window_index,
                "startWindowNumber": start_window_index + 1,
                "keptWindowCount": start_window_index,
                "deletedWindowCount": len(state["windows"]),
                "deletedAttemptCount": len(state["attempts"]),
                "deletedSegmentCount": len(state["segments"]),
                "oldTaskCount": len({row["task_id"] for row in state["attempts"]}),
                "invalidatedMergeCount": len(state["merges"]),
                "sourceStart": start,
                "absoluteStart": (
                    (base_time + timedelta(seconds=start)).isoformat()
                    if base_time else None
                ),
                "previewToken": state["preview_token"],
                "confirmationText": f"从窗口 {start_window_index + 1} 重跑",
            }

    async def start_tail_rebuild(
        self,
        job_id: str,
        start_window_index: int,
        preview_token: str,
        confirmation_text: str,
    ) -> dict[str, Any]:
        required_text = f"从窗口 {start_window_index + 1} 重跑"
        if confirmation_text != required_text:
            raise RebuildValidationError(f"Type exactly: {required_text}")
        async with self._rebuild_lock:
            job = await self.database.get_job(job_id)
            self._validate_tail_rebuild_job(job_id, job)
            try:
                state = await self.database.get_rebuild_preview(
                    job_id, start_window_index
                )
            except ValueError as exc:
                raise RebuildValidationError(str(exc)) from exc
            if state is None:
                raise RebuildValidationError("Job not found")
            if state["preview_token"] != preview_token:
                raise RebuildConflictError(
                    "The job changed after preview; preview it again"
                )

            snapshot_id = f"{utc_now().replace(':', '').replace('+', '-')}-{start_window_index:03d}"
            snapshot_path = (
                self.settings.data_dir / "jobs" / job_id / "rebuilds" / f"{snapshot_id}.json"
            )
            snapshot = {
                "createdAt": utc_now(),
                "job": state["job"],
                "startWindowIndex": start_window_index,
                "previousWindow": state["previous_window"],
                "windows": state["windows"],
                "attempts": state["attempts"],
                "segments": state["segments"],
                "merges": state["merges"],
                "mergeMembers": state["merge_members"],
            }
            try:
                await asyncio.to_thread(self._atomic_json, snapshot_path, snapshot)
            except OSError as exc:
                raise RebuildValidationError(
                    f"Could not save the rebuild snapshot: {exc}"
                ) from exc
            try:
                rebuild = await self.database.truncate_job_for_rebuild(
                    job_id,
                    start_window_index,
                    preview_token,
                    str(snapshot_path),
                )
            except ValueError as exc:
                snapshot_path.unlink(missing_ok=True)
                message = str(exc)
                if "changed after preview" in message:
                    raise RebuildConflictError(message) from exc
                raise RebuildValidationError(message) from exc
            self.notify()
            return self._public_rebuild(rebuild)

    def _validate_tail_rebuild_job(
        self, job_id: str, job: dict[str, Any] | None
    ) -> None:
        if not job:
            raise RebuildValidationError("Job not found")
        if job_id in self._active or job["status"] not in {
            JobStatus.PAUSED.value,
            JobStatus.FAILED.value,
            JobStatus.STOPPED.value,
            JobStatus.COMPLETED.value,
        }:
            raise RebuildConflictError(
                "Pause or finish the job before rebuilding its tail"
            )
        if any(active_job_id == job_id for active_job_id, _ in self._resplit_tasks):
            raise RebuildConflictError("This job has a manual resplit in progress")
        if not job.get("islice_base_url"):
            raise RebuildValidationError("The job has not been assigned to an iSlice instance")
        if not self._source_unchanged(job):
            raise RebuildValidationError("Source file size or modification time changed")

    @staticmethod
    def _public_rebuild(rebuild: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": rebuild["id"],
            "jobId": rebuild["job_id"],
            "startWindowIndex": int(rebuild["start_window_index"]),
            "generation": int(rebuild["generation"]),
            "status": rebuild["status"],
            "errorMessage": rebuild.get("error_message") or "",
            "createdAt": rebuild["created_at"],
            "updatedAt": rebuild["updated_at"],
            "finishedAt": rebuild.get("finished_at"),
        }

    async def schedule_resplit(
        self, job_id: str, window_index: int, expected_task_id: str
    ) -> dict[str, Any]:
        key = (job_id, window_index)
        async with self._resplit_lock:
            if any(active_job_id == job_id for active_job_id, _index in self._resplit_tasks):
                raise ResplitConflictError("This job already has a resplit in progress")
            job = await self.database.get_job(job_id)
            if not job:
                raise ResplitValidationError("Job not found")
            if job_id in self._active or job["status"] in {
                JobStatus.PENDING_SCHEDULE.value,
                JobStatus.QUEUED.value,
                JobStatus.PROBING.value,
                JobStatus.RUNNING.value,
                JobStatus.PAUSE_REQUESTED.value,
                JobStatus.STOP_REQUESTED.value,
            }:
                raise ResplitConflictError(
                    "Pause or finish the job before manually resplitting a window"
                )
            window = await self.database.get_window(job_id, window_index)
            if not window:
                raise ResplitValidationError("Window not found")
            attempts = await self.database.get_attempts(window["id"])
            if not attempts:
                raise ResplitValidationError("This window has not been submitted to iSlice")
            attempt = attempts[-1]
            if attempt["task_id"] != expected_task_id:
                raise ResplitConflictError(
                    "The window task changed; refresh the page before resplitting"
                )
            if attempt["status"] not in {"completed", "failed", "discarded"}:
                raise ResplitConflictError("The iSlice task is still active")
            if not job.get("islice_base_url"):
                raise ResplitValidationError("The job has not been assigned to an iSlice instance")
            if not self._source_unchanged(job):
                raise ResplitValidationError("Source file size or modification time changed")

            await self.database.update_attempt(
                attempt["id"],
                status="resplit_queued",
                service_status="waiting",
                progress=0.0,
                error_message="",
                finished_at=None,
            )
            task = asyncio.create_task(
                self._run_resplit(job_id, window_index, int(attempt["id"])),
                name=f"resplit-{expected_task_id}",
            )
            self._resplit_tasks[key] = task
            task.add_done_callback(
                lambda done, task_key=key: self._resplit_finished(task_key, done)
            )
            return {
                "jobId": job_id,
                "windowIndex": window_index,
                "taskId": expected_task_id,
                "status": "resplit_queued",
            }

    async def accept_resplit_overlap(
        self, job_id: str, window_index: int, expected_task_id: str
    ) -> dict[str, Any]:
        """Commit a completed manual-resplit response despite cross-window overlap.

        This never calls iSlice. It is an explicit recovery action for a manual
        resplit whose response was persisted but rejected by the cross-window
        boundary guard.
        """
        async with self._resplit_lock:
            if any(active_job_id == job_id for active_job_id, _ in self._resplit_tasks):
                raise ResplitConflictError("This job already has a resplit in progress")

            job = await self.database.get_job(job_id)
            if not job:
                raise ResplitValidationError("Job not found")
            window = await self.database.get_window(job_id, window_index)
            if not window:
                raise ResplitValidationError("Window not found")
            attempts = await self.database.get_attempts(window["id"])
            if not attempts:
                raise ResplitValidationError("This window has no iSlice attempt")
            attempt = attempts[-1]
            if attempt["task_id"] != expected_task_id:
                raise ResplitConflictError(
                    "The window task changed; refresh the page before accepting overlap"
                )
            if window["status"] != WindowStatus.FAILED.value:
                raise ResplitConflictError(
                    "This window is not waiting for overlap acceptance"
                )
            if (
                attempt["status"] != "completed"
                or attempt.get("service_status") != "completed"
                or not attempt.get("raw_response_path")
            ):
                raise ResplitValidationError(
                    "No completed manual-resplit response is available"
                )

            boundary_error = str(window.get("error_message") or "")
            allowed_errors = (
                "accepted segments overlap the previous window",
                "the resplit handoff changed from the next window's fixed source start",
                "resplit segments overlap a following window",
            )
            if not any(marker in boundary_error for marker in allowed_errors):
                raise ResplitValidationError(
                    "The failed resplit is not a cross-window overlap conflict"
                )

            raw_path = Path(str(attempt["raw_response_path"]))
            expected_raw_dir = (
                self.settings.data_dir / "jobs" / job_id / "raw"
            ).resolve()
            try:
                resolved_raw_path = raw_path.resolve(strict=True)
                resolved_raw_path.relative_to(expected_raw_dir)
                payload = json.loads(resolved_raw_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                raise ResplitValidationError(
                    "The saved manual-resplit response is unavailable or invalid"
                ) from exc

            task_info = payload.get("taskInfo") or {}
            if (
                task_info.get("taskId") != expected_task_id
                or str(task_info.get("status") or "").lower() != "completed"
            ):
                raise ResplitValidationError(
                    "The saved response does not match the completed iSlice task"
                )

            await self._commit_resplit_payload(
                job,
                window,
                payload,
                allow_overlap=True,
                overlap_reason=boundary_error,
            )

            windows = await self.database.get_windows(job_id)
            all_completed = bool(windows) and all(
                item["status"] == WindowStatus.COMPLETED.value for item in windows
            )
            if all_completed:
                await self.database.update_job(
                    job_id,
                    status=JobStatus.COMPLETED.value,
                    current_window=int(job["total_windows"]),
                    progress=100.0,
                    pause_requested=0,
                    error_message="",
                )
                await self.write_manifest(job_id)

            return {
                "jobId": job_id,
                "windowIndex": window_index,
                "taskId": expected_task_id,
                "status": "overlap_accepted",
            }

    def _resplit_finished(
        self, key: tuple[str, int], task: asyncio.Task[None]
    ) -> None:
        if self._resplit_tasks.get(key) is task:
            self._resplit_tasks.pop(key, None)
        if not task.cancelled() and task.exception() is not None:
            logger.error("Window resplit task failed unexpectedly: %s", task.exception())

    async def _run_resplit(
        self, job_id: str, window_index: int, attempt_id: int
    ) -> None:
        window = await self.database.get_window(job_id, window_index)
        attempt = None
        if window:
            attempt = next(
                (
                    item
                    for item in await self.database.get_attempts(window["id"])
                    if int(item["id"]) == attempt_id
                ),
                None,
            )
        if not window or not attempt:
            return
        try:
            await self._execute_resplit(job_id, window, attempt)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            message = str(exc) or exc.__class__.__name__
            logger.exception(
                "Job %s window %s resplit failed", job_id, window_index + 1
            )
            latest_attempt = next(
                (
                    item
                    for item in await self.database.get_attempts(window["id"])
                    if int(item["id"]) == attempt_id
                ),
                attempt,
            )
            attempt_fields: dict[str, Any] = {
                "error_message": message,
                "finished_at": utc_now(),
            }
            if latest_attempt.get("service_status") != "completed":
                attempt_fields["status"] = "failed"
            await self.database.update_attempt(attempt_id, **attempt_fields)
            await self.database.update_window(
                window["id"],
                status=WindowStatus.FAILED.value,
                error_message=message,
            )
            await self._pause_job(
                job_id, f"Window {window_index + 1} manual resplit failed: {message}"
            )
            await self.write_manifest(job_id)

    async def _execute_resplit(
        self,
        job_id: str,
        window: dict[str, Any],
        attempt: dict[str, Any],
    ) -> None:
        job = await self.database.get_job(job_id)
        if not job:
            raise ResplitValidationError("Job not found")
        if not self._source_unchanged(job):
            raise ResplitValidationError("Source file size or modification time changed")

        index = int(window["window_index"])
        start = float(window["requested_start"])
        end = float(window["nominal_end"])
        chunk_path = self.settings.temp_dir / job_id / f"window-{index:03d}.ts"
        chunk_url = f"{self.settings.public_base_url}/internal/chunks/{job_id}/{index}.ts"
        if not chunk_path.is_file():
            await self.database.update_window(
                window["id"], status=WindowStatus.CUTTING.value, error_message=""
            )
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

        islice_client = self._islice_client(job)
        task_id = str(attempt["task_id"])
        request = self._create_task_request(job, task_id, chunk_url, start)
        gate_url = str(job["islice_base_url"])
        gate_released = False
        await self._await_submission_turn(
            job_id, gate_url, task_id, float(job.get("progress") or 0)
        )
        try:
            await self.database.update_attempt(
                attempt["id"],
                status="resplitting",
                service_status="deleting",
                progress=0.0,
                raw_response_path="",
                error_message="",
                finished_at=None,
            )
            await islice_client.delete_task(task_id)
            existing = await islice_client.ensure_task(task_id, request)
            submitted_at = utc_now()
            service_status, service_progress = self._task_progress(existing)
            await self.database.update_attempt(
                attempt["id"],
                status="resplitting",
                service_status=service_status,
                progress=service_progress,
                submitted_at=submitted_at,
            )
            await self.database.update_window(
                window["id"], status=WindowStatus.POLLING.value
            )
            if self._pipeline_ready(service_status, service_progress):
                gate_released = await self._release_submission_turn(
                    gate_url, task_id
                )
            payload = (
                existing
                if self._is_terminal(existing)
                else await self._poll(
                    job_id,
                    int(attempt["id"]),
                    task_id,
                    islice_client,
                    gate_url,
                    task_id,
                )
            )
            gate_released = await self._release_submission_turn(
                gate_url, task_id
            ) or gate_released
        finally:
            if not gate_released:
                await self._release_submission_turn(gate_url, task_id)

        raw_path = await self._write_raw(
            job_id, index, int(attempt["attempt_no"]), payload
        )
        status, progress = self._task_progress(payload)
        if status != "completed":
            error = str(
                payload.get("taskInfo", {}).get("errorMessage")
                or "iSlice task failed"
            )
            await self.database.update_attempt(
                attempt["id"],
                status="failed",
                service_status=status,
                progress=progress,
                raw_response_path=str(raw_path),
                error_message=error,
                finished_at=utc_now(),
            )
            raise ISliceError(error)

        await self.database.update_attempt(
            attempt["id"],
            status="completed",
            service_status=status,
            progress=progress,
            raw_response_path=str(raw_path),
            error_message="",
            finished_at=utc_now(),
        )
        await self._commit_resplit_payload(job, window, payload)

    async def _commit_resplit_payload(
        self,
        job: dict[str, Any],
        window: dict[str, Any],
        payload: dict[str, Any],
        *,
        allow_overlap: bool = False,
        overlap_reason: str = "",
    ) -> None:
        job_id = str(job["id"])
        index = int(window["window_index"])
        start = float(window["requested_start"])
        end = float(window["nominal_end"])
        base_time = (
            datetime.fromisoformat(job["program_start_time"])
            if job["program_start_time"]
            else None
        )
        processed = process_segments(
            payload.get("segments"),
            window_start=start,
            chunk_duration=end - start,
            is_final_window=index == int(job["total_windows"]) - 1,
            handoff_max_seconds=self.settings.handoff_max_seconds,
            program_start_time=base_time,
        )
        next_start = (
            processed.next_window_start
            if processed.next_window_start is not None
            else end
        )
        if not allow_overlap:
            await self._validate_previous_boundary(job_id, index, processed.segments)
            await self._validate_resplit_next_boundary(
                job_id, index, float(next_start), processed.segments
            )

        await self.database.replace_window_segments(
            job_id, window["id"], processed.segments
        )
        if processed.warning:
            await self.database.append_warning(
                job_id, f"Window {index + 1}: {processed.warning}"
            )
        if allow_overlap:
            await self.database.append_warning(
                job_id,
                f"Window {index + 1}: manual resplit accepted with cross-window "
                f"time overlap; following windows were not rebuilt ({overlap_reason})",
            )
        await self.database.update_window(
            window["id"],
            status=WindowStatus.COMPLETED.value,
            handoff_start=processed.handoff_start,
            error_message="",
        )
        current = await self.database.get_job(job_id)
        if current:
            fields: dict[str, Any] = {"error_message": ""}
            if int(current["current_window"]) <= index:
                completed = index + 1
                fields.update(
                    current_window=completed,
                    next_window_start=float(next_start),
                    progress=min(
                        100.0, completed / int(current["total_windows"]) * 100.0
                    ),
                )
            await self.database.update_job(job_id, **fields)
        await self.write_manifest(job_id)
        chunk_path = self.settings.temp_dir / job_id / f"window-{index:03d}.ts"
        await self._remove_chunk(job_id, {**window, "chunk_path": str(chunk_path)})

    async def _scheduler_loop(self) -> None:
        while not self._stopping:
            capacity = self.settings.max_active_jobs - len(self._active)
            if capacity > 0:
                for job in await self.database.claim_schedulable_jobs(
                    self.settings.configured_islice_urls,
                    capacity,
                ):
                    job_id = job["id"]
                    if job_id in self._active:
                        continue
                    task = asyncio.create_task(self._process_job(job_id), name=f"job-{job_id}")
                    self._active[job_id] = task
                    task.add_done_callback(
                        lambda _task, key=job_id: self._job_finished(key)
                    )
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=1.0)
            except TimeoutError:
                pass

    def _job_finished(self, job_id: str) -> None:
        self._active.pop(job_id, None)
        self.notify()

    async def _process_job(self, job_id: str) -> None:
        try:
            job = await self.database.get_job(job_id)
            if not job or job["status"] != JobStatus.QUEUED.value:
                return
            await self._cancel_job_gate_monitors(job_id)
            islice_client = self._islice_client(job)
            fields: dict[str, Any] = {
                "status": JobStatus.RUNNING.value,
                "pause_requested": 0,
                "error_message": "",
            }
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
                    await self.database.mark_latest_rebuild_completed(job_id)
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

                completed = await self._process_window(job, window, islice_client)
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

    def _islice_client(self, job: dict[str, Any]) -> ISliceClient:
        if isinstance(self.islice, ISlicePool):
            return self.islice.get_client(str(job.get("islice_base_url") or ""))
        return self.islice

    async def _process_window(
        self,
        job: dict[str, Any],
        window: dict[str, Any],
        islice_client: ISliceClient,
    ) -> bool:
        job_id = job["id"]
        index = int(window["window_index"])
        start = float(window["requested_start"])
        end = float(window["nominal_end"])
        job_temp_dir = self.settings.temp_dir / job_id
        generation = int(job.get("rebuild_revision") or 0)
        chunk_suffix = f"-g{generation}" if generation else ""
        chunk_path = job_temp_dir / f"window-{index:03d}{chunk_suffix}.ts"
        chunk_url = (
            f"{self.settings.public_base_url}/internal/chunks/"
            f"{job_id}/{generation}/{index}.ts"
        )

        if not chunk_path.is_file():
            await self.database.update_window(window["id"], status=WindowStatus.CUTTING.value)
            try:
                await self.media.cut(
                    Path(job["source_path"]),
                    chunk_path,
                    start,
                    end,
                    CutMode(job["cut_mode"]),
                )
            except MediaError as exc:
                message = f"Window {index + 1} media preparation failed: {exc}"
                await self.database.update_window(
                    window["id"],
                    status=WindowStatus.FAILED.value,
                    error_message=str(exc),
                )
                await self._pause_job(job_id, message)
                return False
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
                (
                    attempt
                    for attempt in attempt_items
                    if attempt["status"] not in {"failed", "discarded"}
                ),
                None,
            )
            first_attempt_no = (
                int(reusable["attempt_no"])
                if reusable
                else max(attempts, default=0) + 1
            )
            attempt_no = first_attempt_no
            if await self._control_requested(job_id):
                return False
            attempt = attempts.get(attempt_no)
            if not attempt:
                generation_part = f"-g{generation}" if generation else ""
                task_id = (
                    f"sh-{job_id[:16]}-w{index:03d}{generation_part}-a{attempt_no}"
                )
                attempt = await self.database.create_attempt(
                    window["id"], attempt_no, task_id
                )

            request = self._create_task_request(
                job, attempt["task_id"], chunk_url, start
            )
            gate_url = str(job.get("islice_base_url") or self.settings.islice_base_url)
            gate_ticket = str(attempt["task_id"])
            gate_released = float(attempt.get("progress") or 0) >= (
                self.settings.pipeline_progress_threshold
            )
            failure_message = ""
            try:
                if not gate_released:
                    await self._await_submission_turn(
                        job_id,
                        gate_url,
                        gate_ticket,
                        float(job.get("progress") or 0),
                    )
                existing = await islice_client.ensure_task(attempt["task_id"], request)
                await self._raise_if_control_requested(job_id)
                service_status, service_progress = self._task_progress(existing)
                attempt_fields: dict[str, Any] = {
                    "status": "polling",
                    "service_status": service_status,
                    "progress": service_progress,
                    "error_message": "",
                }
                if not attempt.get("submitted_at"):
                    attempt_fields["submitted_at"] = utc_now()
                await self.database.update_attempt(
                    attempt["id"],
                    **attempt_fields,
                )
                await self.database.update_window(
                    window["id"], status=WindowStatus.POLLING.value
                )
                if self._pipeline_ready(service_status, service_progress):
                    gate_released = await self._release_submission_turn(
                        gate_url, gate_ticket
                    ) or gate_released
                payload = (
                    existing
                    if self._is_terminal(existing)
                    else await self._poll(
                        job_id,
                        attempt["id"],
                        attempt["task_id"],
                        islice_client,
                        gate_url,
                        gate_ticket,
                    )
                )
                gate_released = await self._release_submission_turn(
                    gate_url, gate_ticket
                ) or gate_released
                raw_path = await self._write_raw(job_id, index, attempt_no, payload)
                status = str(payload["taskInfo"].get("status") or "")
                if status == "completed":
                    await self.database.update_attempt(
                        attempt["id"],
                        status="completed",
                        service_status=status,
                        progress=self._task_progress(payload)[1],
                        raw_response_path=str(raw_path),
                        finished_at=utc_now(),
                    )
                    terminal_payload = payload
                    terminal_attempt = attempt
                else:
                    failure_message = str(
                        payload["taskInfo"].get("errorMessage")
                        or "iSlice task failed"
                    )
                    await self.database.update_attempt(
                        attempt["id"],
                        status="failed",
                        service_status=status,
                        progress=self._task_progress(payload)[1],
                        raw_response_path=str(raw_path),
                        error_message=failure_message,
                        finished_at=utc_now(),
                    )
            except JobControlRequested:
                if not gate_released:
                    self._start_gate_monitor(
                        gate_url, gate_ticket, islice_client
                    )
                return False
            except (ISliceError, TimeoutError) as exc:
                failure_message = str(exc)
                gate_released = await self._release_submission_turn(
                    gate_url, gate_ticket
                ) or gate_released
                await self.database.update_attempt(
                    attempt["id"],
                    status="failed",
                    error_message=failure_message,
                    finished_at=utc_now(),
                )

            if terminal_payload is None:
                failure_message = failure_message or "iSlice task failed"
                await self.database.update_window(
                    window["id"],
                    status=WindowStatus.FAILED.value,
                    error_message=failure_message,
                )
                await self._pause_job(
                    job_id, f"Window {index + 1} iSlice task failed: {failure_message}"
                )
                return False

        if terminal_payload is None or terminal_attempt is None:
            await self.database.update_window(
                window["id"],
                status=WindowStatus.FAILED.value,
                error_message="iSlice task result is unavailable",
            )
            await self._pause_job(
                job_id, f"Window {index + 1} iSlice task result is unavailable"
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

    async def _poll(
        self,
        job_id: str,
        attempt_id: int,
        task_id: str,
        islice_client: ISliceClient,
        gate_url: str,
        gate_ticket: str,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + self.settings.window_timeout_seconds
        consecutive_errors = 0
        while time.monotonic() < deadline:
            await self._raise_if_control_requested(job_id)
            try:
                payload = await islice_client.get_task_info(task_id)
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
            service_status, service_progress = self._task_progress(payload)
            await self.database.update_attempt(
                attempt_id,
                service_status=service_status,
                progress=service_progress,
            )
            if self._pipeline_ready(service_status, service_progress):
                await self._release_submission_turn(gate_url, gate_ticket)
            await self._raise_if_control_requested(job_id)
            if self._is_terminal(payload):
                return payload
            await self._sleep_with_control(job_id, self.settings.poll_interval_seconds)
        raise TimeoutError(f"Task {task_id} exceeded the window timeout")

    @staticmethod
    def _task_progress(payload: dict[str, Any] | None) -> tuple[str, float]:
        task_info = payload.get("taskInfo", {}) if payload else {}
        status = str(task_info.get("status") or "")
        raw_progress = task_info.get("progress")
        if raw_progress is None and status == "completed":
            return status, 100.0
        try:
            progress = float(raw_progress or 0)
        except (TypeError, ValueError):
            progress = 0.0
        return status, min(100.0, max(0.0, progress))

    def _pipeline_ready(self, status: str, progress: float) -> bool:
        return status in {"completed", "failed"} or (
            progress >= self.settings.pipeline_progress_threshold
        )

    async def _await_submission_turn(
        self,
        job_id: str,
        base_url: str,
        ticket: str,
        priority: float,
    ) -> None:
        waiter = asyncio.create_task(
            self._submission_gate.acquire(base_url, ticket, priority),
            name=f"islice-gate-{ticket}",
        )
        try:
            while not waiter.done():
                done, _pending = await asyncio.wait({waiter}, timeout=1.0)
                if done:
                    break
                await self._raise_if_control_requested(job_id)
            await waiter
        except BaseException:
            waiter.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await waiter
            raise

    async def _release_submission_turn(self, base_url: str, ticket: str) -> bool:
        released = await self._submission_gate.release(base_url, ticket)
        if released:
            self.notify()
        return released

    def _start_gate_monitor(
        self,
        base_url: str,
        ticket: str,
        islice_client: ISliceClient,
    ) -> None:
        existing = self._gate_monitors.get(ticket)
        if existing and not existing.done():
            return
        task = asyncio.create_task(
            self._monitor_gate_release(base_url, ticket, islice_client),
            name=f"islice-gate-monitor-{ticket}",
        )
        self._gate_monitors[ticket] = task
        task.add_done_callback(
            lambda done, key=ticket: self._remove_gate_monitor(key, done)
        )

    def _remove_gate_monitor(self, ticket: str, task: asyncio.Task[None]) -> None:
        if self._gate_monitors.get(ticket) is task:
            self._gate_monitors.pop(ticket, None)

    async def _cancel_job_gate_monitors(self, job_id: str) -> None:
        prefix = f"sh-{job_id[:16]}-"
        tasks = [
            task
            for ticket, task in self._gate_monitors.items()
            if ticket.startswith(prefix)
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _monitor_gate_release(
        self,
        base_url: str,
        ticket: str,
        islice_client: ISliceClient,
    ) -> None:
        try:
            while not self._stopping:
                try:
                    payload = await islice_client.get_task_info(ticket)
                except ISliceError:
                    await asyncio.sleep(self.settings.poll_interval_seconds)
                    continue
                status, progress = self._task_progress(payload)
                if payload is None or self._pipeline_ready(status, progress):
                    await self._release_submission_turn(base_url, ticket)
                    return
                await asyncio.sleep(self.settings.poll_interval_seconds)
        except asyncio.CancelledError:
            raise

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

    async def _validate_resplit_next_boundary(
        self,
        job_id: str,
        window_index: int,
        next_window_start: float,
        segments: list[dict[str, Any]],
    ) -> None:
        windows = await self.database.get_windows(job_id)
        next_window = next(
            (
                item
                for item in windows
                if int(item["window_index"]) == window_index + 1
            ),
            None,
        )
        if not next_window:
            return
        existing_start = float(next_window["requested_start"])
        tolerance = self.settings.window_boundary_tolerance_seconds
        if abs(next_window_start - existing_start) > tolerance:
            raise SegmentValidationError(
                "the resplit handoff changed from the next window's fixed source start: "
                f"new {next_window_start:.3f}, existing {existing_start:.3f}"
            )

        accepted = [segment for segment in segments if segment["accepted"]]
        if not accepted:
            return
        existing = await self.database.get_segments(job_id, accepted_only=True)
        following = [
            segment
            for segment in existing
            if int(segment["window_index"]) > window_index
        ]
        if not following:
            return
        current_end = max(float(segment["global_end"]) for segment in accepted)
        following_start = min(float(segment["global_start"]) for segment in following)
        if current_end > following_start + tolerance:
            raise SegmentValidationError(
                "resplit segments overlap a following window: "
                f"current end {current_end:.3f}, following start {following_start:.3f}"
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
        merges = await self.database.get_job_merges(job_id, include_inactive=True)
        for segment in segments:
            segment["accepted"] = bool(segment["accepted"])
            segment["ignored"] = bool(segment["ignored"])
            segment["keywords"] = json.loads(segment.pop("keywords_json"))
            segment["raw"] = json.loads(segment.pop("raw_json"))
        manifest = {
            "schemaVersion": 6,
            "generatedAt": utc_now(),
            "externalMediaMayExpire": True,
            "job": {
                **self._manifest_job(job),
            },
            "windows": windows,
            "attempts": attempts,
            "segments": segments,
            "manualMerges": merges,
        }
        path = self.settings.data_dir / "jobs" / job_id / "result.json"
        await asyncio.to_thread(self._atomic_json, path, manifest)
        return manifest

    @staticmethod
    def _manifest_job(job: dict[str, Any]) -> dict[str, Any]:
        payload = dict(job)
        payload["pause_requested"] = bool(payload["pause_requested"])
        payload["stop_requested"] = bool(payload["stop_requested"])
        payload["reviewed"] = bool(payload.get("reviewed"))
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
