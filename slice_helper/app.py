from __future__ import annotations

import asyncio
import json
import logging
import shutil
import sqlite3
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from datetime import date
from typing import Any
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import __version__
from .config import Settings
from .database import Database
from .excel_export import build_channel_workbook, safe_export_filename
from .islice import ISlicePool
from .media import MediaError, MediaService
from .models import (
    CONTENT_TYPES,
    ChannelCreate,
    ChannelUpdate,
    JobReviewUpdate,
    JobCreate,
    JobStatus,
    SegmentUpdate,
    TimeReferenceUpdate,
    TailRebuildRequest,
    WindowResplitRequest,
)
from .orchestrator import (
    Orchestrator,
    RebuildConflictError,
    RebuildValidationError,
    ResplitConflictError,
    ResplitValidationError,
)
from .processing import calculate_total_windows
from .source_download import HttpSourceDownloader, SourceDownloadError


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

PACKAGE_DIR = Path(__file__).resolve().parent
OCR_FRAME_OFFSETS = tuple(float(minutes * 60) for minutes in range(6))


def _public_job(job: dict[str, Any]) -> dict[str, Any]:
    result = dict(job)
    result["pause_requested"] = bool(result.get("pause_requested"))
    result["stop_requested"] = bool(result.get("stop_requested"))
    result["reviewed"] = bool(result.get("reviewed"))
    result["warnings"] = json.loads(result.pop("warnings_json", "[]") or "[]")
    result["accepted_segment_count"] = int(result.get("accepted_segment_count") or 0)
    result["window_count"] = int(result.get("window_count") or 0)
    return result


def _public_channel(channel: dict[str, Any]) -> dict[str, Any]:
    result = dict(channel)
    if "job_count" in result:
        result["job_count"] = int(result["job_count"] or 0)
    result.pop("normalized_name", None)
    return result


def create_app(settings: Settings | None = None) -> FastAPI:
    configured = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        configured.data_dir.mkdir(parents=True, exist_ok=True)
        configured.temp_dir.mkdir(parents=True, exist_ok=True)
        database = Database(configured.database_path)
        await database.initialize()
        media = MediaService(configured)
        source_downloader = HttpSourceDownloader()
        await database.assign_legacy_jobs_with_attempts(configured.islice_base_url)
        islice = ISlicePool(configured)
        orchestrator = Orchestrator(configured, database, media, islice)
        app.state.settings = configured
        app.state.database = database
        app.state.media = media
        app.state.source_downloader = source_downloader
        app.state.islice = islice
        app.state.orchestrator = orchestrator
        await orchestrator.start()
        try:
            yield
        finally:
            await orchestrator.stop()
            await islice.close()

    application = FastAPI(
        title="TS Continuous Slice Helper",
        version=__version__,
        lifespan=lifespan,
    )
    templates = Jinja2Templates(directory=str(PACKAGE_DIR / "templates"))
    application.mount("/static", StaticFiles(directory=str(PACKAGE_DIR / "static")), name="static")

    @application.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def index(request: Request):
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={"version": application.version},
        )

    @application.get("/api/channels")
    async def list_channels(request: Request):
        channels = await request.app.state.database.list_channels()
        return [_public_channel(channel) for channel in channels]

    @application.post("/api/channels", status_code=201)
    async def create_channel(request: Request, body: ChannelCreate):
        try:
            channel = await request.app.state.database.create_channel(body.name)
        except sqlite3.IntegrityError as exc:
            raise HTTPException(status_code=409, detail="频道名已存在") from exc
        return _public_channel(channel)

    @application.patch("/api/channels/{channel_id}")
    async def update_channel(request: Request, channel_id: str, body: ChannelUpdate):
        try:
            channel = await request.app.state.database.update_channel(channel_id, body.name)
        except sqlite3.IntegrityError as exc:
            raise HTTPException(status_code=409, detail="频道名已存在") from exc
        if channel is None:
            raise HTTPException(status_code=404, detail="频道不存在")
        return _public_channel(channel)

    @application.delete("/api/channels/{channel_id}", status_code=204)
    async def delete_channel(request: Request, channel_id: str):
        try:
            deleted = await request.app.state.database.delete_channel(channel_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail="该频道已有作业，不能删除") from exc
        if not deleted:
            raise HTTPException(status_code=404, detail="频道不存在")
        return Response(status_code=204)

    @application.get("/api/channels/{channel_id}/export.xlsx")
    async def export_channel(request: Request, channel_id: str):
        export = await request.app.state.database.get_channel_export(channel_id)
        if export is None:
            raise HTTPException(status_code=404, detail="频道不存在")
        content = await asyncio.to_thread(build_channel_workbook, export)
        filename = safe_export_filename(str(export["channel"]["name"]))
        return Response(
            content=content,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={
                "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"
            },
        )

    @application.post("/api/jobs", status_code=201)
    async def create_job(request: Request, body: JobCreate):
        channel = await request.app.state.database.get_channel(body.channel_id)
        if channel is None:
            raise HTTPException(status_code=400, detail="请选择有效频道")
        broadcast_date = body.broadcast_date.isoformat()
        existing = await request.app.state.database.get_current_job_for_channel_date(
            body.channel_id, broadcast_date
        )
        if existing and not body.overwrite:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "channel_date_exists",
                    "message": "该频道在此日期已有作业，是否覆盖？",
                    "jobId": existing["id"],
                },
            )
        if existing and existing["status"] not in {
            JobStatus.COMPLETED.value,
            JobStatus.FAILED.value,
            JobStatus.STOPPED.value,
        }:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "channel_date_active",
                    "message": "该日期的现有作业尚未结束，请先停止后再覆盖",
                    "jobId": existing["id"],
                },
            )
        job_id = uuid.uuid4().hex
        source_url = ""
        managed_source = urlsplit(body.source_path).scheme.lower() in {"http", "https"}
        job_dir = configured.data_dir / "jobs" / job_id
        if managed_source:
            source_url = body.source_path
            source = job_dir / "source.ts"
            try:
                await request.app.state.source_downloader.download(source_url, source)
            except SourceDownloadError as exc:
                shutil.rmtree(job_dir, ignore_errors=True)
                raise HTTPException(status_code=502, detail=str(exc)) from exc
        else:
            source = Path(body.source_path).expanduser().resolve()
            if not source.is_file():
                raise HTTPException(
                    status_code=400,
                    detail="sourcePath does not exist or is not a file",
                )
        try:
            stat = source.stat()
            probe = await request.app.state.media.probe(source)
        except (OSError, MediaError) as exc:
            if managed_source:
                shutil.rmtree(job_dir, ignore_errors=True)
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if "mpegts" not in probe.format_name.lower():
            if managed_source:
                shutil.rmtree(job_dir, ignore_errors=True)
            raise HTTPException(
                status_code=400,
                detail=f"The file content is not MPEG-TS: {probe.format_name}",
            )
        warnings: list[str] = []
        frame_path = configured.data_dir / "jobs" / job_id / "time-reference.png"
        time_reference = None
        time_reference_errors: list[str] = []
        for frame_offset in OCR_FRAME_OFFSETS:
            try:
                time_reference = await request.app.state.media.detect_time_reference(
                    source,
                    frame_path,
                    frame_offset_seconds=frame_offset,
                )
                break
            except MediaError as exc:
                time_reference_errors.append(f"{int(frame_offset)}s: {exc}")
        time_reference_error = "; ".join(time_reference_errors)

        if time_reference is not None:
            resolved_start_time = time_reference.source_start_time
            time_reference_source = "ocr"
            time_reference_error = ""
            if time_reference.frame_offset_seconds:
                warnings.append(
                    "Time OCR succeeded at source offset "
                    f"{int(time_reference.frame_offset_seconds)}s after "
                    f"{len(time_reference_errors) + 1} attempts"
                )
            if body.program_start_time is not None:
                try:
                    difference = abs(
                        (body.program_start_time - resolved_start_time).total_seconds()
                    )
                except TypeError:
                    difference = None
                if difference is None or difference > 1.0:
                    warnings.append(
                        "OCR time overrides the supplied programStartTime fallback: "
                        f"{resolved_start_time.isoformat()}"
                    )
        elif body.program_start_time is not None:
            resolved_start_time = body.program_start_time
            time_reference_source = "manual_fallback"
            warnings.append(f"Time OCR failed; programStartTime fallback used: {time_reference_error}")
        else:
            resolved_start_time = None
            time_reference_source = "unavailable"
            warnings.append(f"Time OCR failed after 6 attempts: {time_reference_error}")

        stopped_for_missing_time = resolved_start_time is None
        missing_time_message = (
            "Time OCR failed after 6 attempts and programStartTime was not supplied; "
            "job stopped"
        )

        total_windows = calculate_total_windows(
            probe.duration,
            configured.window_seconds,
            configured.window_boundary_tolerance_seconds,
        )
        try:
            created_job = await request.app.state.database.create_job(
                {
                "id": job_id,
                "source_path": str(source),
                "source_size": stat.st_size,
                "source_mtime_ns": stat.st_mtime_ns,
                "source_duration": probe.duration,
                "source_url": source_url,
                "islice_base_url": "",
                "template_id": body.template_id,
                "language": body.language,
                "channel_id": channel["id"],
                "channel_name": channel["name"],
                "broadcast_date": broadcast_date,
                "program_start_time": (
                    resolved_start_time.isoformat() if resolved_start_time else None
                ),
                "time_reference_source": time_reference_source,
                "time_reference_text": (
                    time_reference.matched_text if time_reference is not None else ""
                ),
                "time_reference_confidence": (
                    time_reference.confidence if time_reference is not None else None
                ),
                "time_reference_frame_path": (
                    str(frame_path)
                    if time_reference is not None and frame_path.is_file()
                    else ""
                ),
                "time_reference_frame_offset": (
                    time_reference.frame_offset_seconds if time_reference is not None else 0.0
                ),
                "time_reference_error": time_reference_error,
                "cut_mode": body.cut_mode.value,
                "total_windows": total_windows,
                "warnings_json": json.dumps(warnings, ensure_ascii=False),
                "status": (
                    JobStatus.STOPPED.value
                    if stopped_for_missing_time
                    else JobStatus.PENDING_SCHEDULE.value
                ),
                "error_message": missing_time_message if stopped_for_missing_time else "",
                },
                supersede_job_id=existing["id"] if existing else None,
            )
        except sqlite3.IntegrityError as exc:
            if managed_source:
                shutil.rmtree(job_dir, ignore_errors=True)
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "channel_date_exists",
                    "message": "该频道在此日期已有作业，请刷新后重试",
                },
            ) from exc
        request.app.state.orchestrator.notify()
        return _public_job(created_job)

    @application.get("/api/jobs")
    async def list_jobs(
        request: Request,
        status: str | None = Query(default=None),
        channel_id: str | None = Query(default=None, alias="channelId"),
        broadcast_date: str | None = Query(default=None, alias="broadcastDate"),
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=20, alias="pageSize", ge=1, le=100),
    ):
        if status and status not in {item.value for item in JobStatus}:
            raise HTTPException(status_code=400, detail="Unknown job status")
        if broadcast_date:
            try:
                date.fromisoformat(broadcast_date)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="业务日期格式应为 YYYY-MM-DD") from exc
        jobs, total = await request.app.state.database.paginate_jobs(
            page=page,
            page_size=page_size,
            status=status,
            channel_id=channel_id,
            broadcast_date=broadcast_date,
        )
        total_pages = max(1, (total + page_size - 1) // page_size)
        if page > total_pages:
            page = total_pages
            jobs, _ = await request.app.state.database.paginate_jobs(
                page=page,
                page_size=page_size,
                status=status,
                channel_id=channel_id,
                broadcast_date=broadcast_date,
            )
        return {
            "items": [_public_job(job) for job in jobs],
            "page": page,
            "pageSize": page_size,
            "total": total,
            "totalPages": total_pages,
        }

    @application.get("/api/jobs/{job_id}")
    async def get_job(request: Request, job_id: str):
        job = await request.app.state.database.get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        windows = await request.app.state.database.get_windows(job_id)
        attempts = await request.app.state.database.get_attempts_for_job(job_id)
        rebuild = await request.app.state.database.get_latest_job_rebuild(job_id)
        return {
            "job": _public_job(job),
            "windows": windows,
            "attempts": attempts,
            "rebuild": (
                request.app.state.orchestrator._public_rebuild(rebuild)
                if rebuild else None
            ),
        }

    @application.patch("/api/jobs/{job_id}/review")
    async def update_job_review(
        request: Request, job_id: str, body: JobReviewUpdate
    ):
        if not await request.app.state.database.get_job(job_id):
            raise HTTPException(status_code=404, detail="Job not found")
        await request.app.state.database.update_job(
            job_id, reviewed=int(body.reviewed)
        )
        updated = await request.app.state.database.get_job(job_id)
        await request.app.state.orchestrator.write_manifest(job_id)
        return _public_job(updated)

    @application.get("/api/jobs/{job_id}/segments")
    async def get_segments(
        request: Request,
        job_id: str,
        accepted_only: bool = Query(default=False, alias="acceptedOnly"),
    ):
        if not await request.app.state.database.get_job(job_id):
            raise HTTPException(status_code=404, detail="Job not found")
        rows = await request.app.state.database.get_segments(job_id, accepted_only=accepted_only)
        for row in rows:
            row["accepted"] = bool(row["accepted"])
            row["ignored"] = bool(row["ignored"])
            row["keywords"] = json.loads(row.pop("keywords_json"))
            row["raw"] = json.loads(row.pop("raw_json"))
        return rows

    @application.patch("/api/jobs/{job_id}/segments/{segment_id}")
    async def update_segment(
        request: Request, job_id: str, segment_id: int, body: SegmentUpdate
    ):
        existing = await request.app.state.database.get_segment(job_id, segment_id)
        if existing is None:
            raise HTTPException(status_code=404, detail="Segment not found")
        if (
            "content_type" in body.model_fields_set
            and body.content_type not in CONTENT_TYPES
        ):
            try:
                task_info = json.loads(existing.get("raw_json") or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                task_info = {}
            if not isinstance(task_info, dict):
                task_info = {}
            task_content_type = str(task_info.get("contentType") or "")
            if not body.restored_from_task or body.content_type != task_content_type:
                raise HTTPException(
                    status_code=422,
                    detail="节目类型必须从预设选项中选择",
                )
        fields: dict[str, Any] = {}
        if "title" in body.model_fields_set:
            fields["title"] = body.title or ""
        if "content_type" in body.model_fields_set:
            fields["content_type"] = body.content_type or ""
        if "ignored" in body.model_fields_set:
            fields["ignored"] = int(bool(body.ignored))
        updated = await request.app.state.database.update_segment(
            job_id, segment_id, **fields
        )
        if updated is None:
            raise HTTPException(status_code=404, detail="Segment not found")
        await request.app.state.orchestrator.write_manifest(job_id)
        updated["accepted"] = bool(updated["accepted"])
        updated["ignored"] = bool(updated["ignored"])
        updated["keywords"] = json.loads(updated.pop("keywords_json"))
        updated["raw"] = json.loads(updated.pop("raw_json"))
        return updated

    @application.get("/api/jobs/{job_id}/segments/{segment_id}/task-values")
    async def get_segment_task_values(
        request: Request, job_id: str, segment_id: int
    ):
        segment = await request.app.state.database.get_segment(job_id, segment_id)
        if segment is None:
            raise HTTPException(status_code=404, detail="Segment not found")
        try:
            task_info = json.loads(segment.get("raw_json") or "{}")
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=409, detail="任务信息无法读取") from exc
        if not isinstance(task_info, dict):
            raise HTTPException(status_code=409, detail="任务信息无法读取")
        if "title" not in task_info and "contentType" not in task_info:
            raise HTTPException(status_code=409, detail="任务信息中没有可还原的内容")
        return {
            "title": str(task_info.get("title") or ""),
            "contentType": str(task_info.get("contentType") or ""),
        }

    @application.patch("/api/jobs/{job_id}/time-reference")
    async def update_job_time_reference(
        request: Request, job_id: str, body: TimeReferenceUpdate
    ):
        updated, segment_count = await request.app.state.database.update_time_reference(
            job_id, body.program_start_time.isoformat()
        )
        if updated is None:
            raise HTTPException(status_code=404, detail="Job not found")
        await request.app.state.orchestrator.write_manifest(job_id)
        request.app.state.orchestrator.notify()
        return {
            "job": _public_job(updated),
            "updatedSegmentCount": segment_count,
        }

    @application.post(
        "/api/jobs/{job_id}/windows/{window_index}/resplit",
        status_code=202,
    )
    async def resplit_window(
        request: Request,
        job_id: str,
        window_index: int,
        body: WindowResplitRequest,
    ):
        try:
            return await request.app.state.orchestrator.schedule_resplit(
                job_id, window_index, body.task_id
            )
        except ResplitConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ResplitValidationError as exc:
            status_code = 404 if str(exc) in {"Job not found", "Window not found"} else 400
            raise HTTPException(status_code=status_code, detail=str(exc)) from exc

    @application.post(
        "/api/jobs/{job_id}/windows/{window_index}/accept-overlap",
    )
    async def accept_resplit_overlap(
        request: Request,
        job_id: str,
        window_index: int,
        body: WindowResplitRequest,
    ):
        try:
            return await request.app.state.orchestrator.accept_resplit_overlap(
                job_id, window_index, body.task_id
            )
        except ResplitConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ResplitValidationError as exc:
            status_code = 404 if str(exc) in {"Job not found", "Window not found"} else 400
            raise HTTPException(status_code=status_code, detail=str(exc)) from exc

    @application.get(
        "/api/jobs/{job_id}/windows/{window_index}/tail-rebuild-preview"
    )
    async def preview_tail_rebuild(
        request: Request, job_id: str, window_index: int
    ):
        try:
            return await request.app.state.orchestrator.preview_tail_rebuild(
                job_id, window_index
            )
        except RebuildConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except RebuildValidationError as exc:
            status_code = 404 if str(exc) in {"Job not found", "Window not found"} else 400
            raise HTTPException(status_code=status_code, detail=str(exc)) from exc
        except ValueError as exc:
            status_code = 404 if str(exc) == "Window not found" else 400
            raise HTTPException(status_code=status_code, detail=str(exc)) from exc

    @application.post(
        "/api/jobs/{job_id}/windows/{window_index}/tail-rebuild",
        status_code=202,
    )
    async def start_tail_rebuild(
        request: Request,
        job_id: str,
        window_index: int,
        body: TailRebuildRequest,
    ):
        try:
            return await request.app.state.orchestrator.start_tail_rebuild(
                job_id,
                window_index,
                body.preview_token,
                body.confirmation_text,
            )
        except RebuildConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except RebuildValidationError as exc:
            status_code = 404 if str(exc) in {"Job not found", "Window not found"} else 400
            raise HTTPException(status_code=status_code, detail=str(exc)) from exc

    @application.post(
        "/api/jobs/{job_id}/tail-rebuild/retry-cleanup",
        status_code=202,
    )
    async def retry_tail_rebuild_cleanup(request: Request, job_id: str):
        try:
            return await request.app.state.orchestrator.retry_tail_rebuild_cleanup(job_id)
        except RebuildConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except RebuildValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @application.get("/api/jobs/{job_id}/result")
    async def get_result(request: Request, job_id: str, download: bool = Query(default=False)):
        if not await request.app.state.database.get_job(job_id):
            raise HTTPException(status_code=404, detail="Job not found")
        manifest = await request.app.state.orchestrator.write_manifest(job_id)
        if download:
            path = configured.data_dir / "jobs" / job_id / "result.json"
            return FileResponse(
                path,
                media_type="application/json",
                filename=f"slice-result-{job_id}.json",
            )
        return manifest

    @application.post("/api/jobs/{job_id}/pause")
    async def pause_job(request: Request, job_id: str):
        job = await _require_job(request, job_id)
        if job["status"] in {
            JobStatus.PENDING_SCHEDULE.value,
            JobStatus.QUEUED.value,
        }:
            await request.app.state.database.update_job(
                job_id, status=JobStatus.PAUSED.value, pause_requested=0
            )
        elif job["status"] == JobStatus.RUNNING.value:
            await request.app.state.database.update_job(
                job_id, status=JobStatus.PAUSE_REQUESTED.value, pause_requested=1
            )
        elif job["status"] not in {JobStatus.PAUSED.value, JobStatus.PAUSE_REQUESTED.value}:
            raise HTTPException(status_code=409, detail="Job cannot be paused in its current state")
        return _public_job((await request.app.state.database.get_job(job_id)) or job)

    @application.post("/api/jobs/{job_id}/resume")
    async def resume_job(request: Request, job_id: str):
        job = await _require_job(request, job_id)
        rebuild = await request.app.state.database.get_latest_job_rebuild(job_id)
        if rebuild and rebuild["status"] in {"deleting", "cleanup_failed"}:
            raise HTTPException(
                status_code=409,
                detail="Old iSlice tasks must be cleaned before this job can resume",
            )
        if job["status"] not in {JobStatus.PAUSED.value, JobStatus.FAILED.value}:
            raise HTTPException(status_code=409, detail="Only paused or failed jobs can be resumed")
        await request.app.state.database.update_job(
            job_id,
            status=JobStatus.PENDING_SCHEDULE.value,
            pause_requested=0,
            stop_requested=0,
            error_message="",
        )
        request.app.state.orchestrator.notify()
        return _public_job((await request.app.state.database.get_job(job_id)) or job)

    @application.post("/api/jobs/{job_id}/stop")
    async def stop_job(request: Request, job_id: str):
        job = await _require_job(request, job_id)
        rebuild = await request.app.state.database.get_latest_job_rebuild(job_id)
        if rebuild and rebuild["status"] in {"deleting", "cleanup_failed"}:
            raise HTTPException(
                status_code=409,
                detail="Old iSlice task cleanup cannot be stopped; retry cleanup if it failed",
            )
        if job["status"] in {
            JobStatus.COMPLETED.value,
            JobStatus.STOPPED.value,
        }:
            raise HTTPException(status_code=409, detail="Job is already terminal")
        if job["status"] in {
            JobStatus.PENDING_SCHEDULE.value,
            JobStatus.QUEUED.value,
            JobStatus.PAUSED.value,
            JobStatus.FAILED.value,
        }:
            await request.app.state.database.update_job(
                job_id, status=JobStatus.STOPPED.value, stop_requested=0
            )
        else:
            await request.app.state.database.update_job(
                job_id, status=JobStatus.STOP_REQUESTED.value, stop_requested=1
            )
        return _public_job((await request.app.state.database.get_job(job_id)) or job)

    @application.get("/internal/chunks/{job_id}/{window_index}.ts", include_in_schema=False)
    async def get_chunk(request: Request, job_id: str, window_index: int):
        window = await request.app.state.database.get_window(job_id, window_index)
        if not window or not window.get("chunk_path"):
            raise HTTPException(status_code=404, detail="Chunk not found")
        path = Path(window["chunk_path"])
        if not path.is_file():
            raise HTTPException(status_code=404, detail="Chunk is no longer available")
        return FileResponse(
            path,
            media_type="video/mp2t",
            filename=f"{job_id}-window-{window_index:03d}.ts",
        )

    @application.get(
        "/internal/chunks/{job_id}/{generation}/{window_index}.ts",
        include_in_schema=False,
    )
    async def get_versioned_chunk(
        request: Request, job_id: str, generation: int, window_index: int
    ):
        job = await request.app.state.database.get_job(job_id)
        if not job or int(job.get("rebuild_revision") or 0) != generation:
            raise HTTPException(status_code=404, detail="Chunk generation not found")
        window = await request.app.state.database.get_window(job_id, window_index)
        if not window or not window.get("chunk_path"):
            raise HTTPException(status_code=404, detail="Chunk not found")
        path = Path(window["chunk_path"])
        if not path.is_file():
            raise HTTPException(status_code=404, detail="Chunk is no longer available")
        return FileResponse(
            path,
            media_type="video/mp2t",
            filename=f"{job_id}-g{generation}-window-{window_index:03d}.ts",
        )

    @application.get("/health/live")
    async def live():
        return {"status": "ok"}

    @application.get("/health/ready")
    async def ready(request: Request):
        checks: dict[str, Any] = {}
        database_ok = await request.app.state.database.ping()
        checks["database"] = "ok" if database_ok else "unavailable"
        media_ok, media_message = await request.app.state.media.tools_ready()
        checks["mediaTools"] = media_message
        ocr_ok, ocr_message = request.app.state.media.ocr_ready()
        checks["timeOcr"] = ocr_message
        islice_ok, islice_messages = await request.app.state.islice.ping()
        checks["iSlice"] = islice_messages
        ready_status = database_ok and media_ok and ocr_ok and islice_ok
        payload = {"status": "ok" if ready_status else "not_ready", "checks": checks}
        return JSONResponse(payload, status_code=200 if ready_status else 503)

    return application


async def _require_job(request: Request, job_id: str) -> dict[str, Any]:
    job = await request.app.state.database.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


app = create_app()
