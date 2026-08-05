from __future__ import annotations

import json
import logging
import shutil
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import __version__
from .config import Settings
from .database import Database
from .islice import ISlicePool
from .media import MediaError, MediaService
from .models import JobCreate, JobStatus
from .orchestrator import Orchestrator
from .processing import calculate_total_windows
from .source_download import HttpSourceDownloader, SourceDownloadError


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

PACKAGE_DIR = Path(__file__).resolve().parent


def _public_job(job: dict[str, Any]) -> dict[str, Any]:
    result = dict(job)
    result["pause_requested"] = bool(result.get("pause_requested"))
    result["stop_requested"] = bool(result.get("stop_requested"))
    result["warnings"] = json.loads(result.pop("warnings_json", "[]") or "[]")
    result["accepted_segment_count"] = int(result.get("accepted_segment_count") or 0)
    result["window_count"] = int(result.get("window_count") or 0)
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

    @application.post("/api/jobs", status_code=201)
    async def create_job(request: Request, body: JobCreate):
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
        time_reference_error = ""
        try:
            time_reference = await request.app.state.media.detect_time_reference(
                source, frame_path
            )
        except MediaError as exc:
            time_reference_error = str(exc)

        if time_reference is not None:
            resolved_start_time = time_reference.source_start_time
            time_reference_source = "ocr"
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
            warnings.append(f"Time OCR failed; real times are unavailable: {time_reference_error}")

        total_windows = calculate_total_windows(
            probe.duration,
            configured.window_seconds,
            configured.window_boundary_tolerance_seconds,
        )
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
                "channel_name": body.channel_name or "",
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
                "time_reference_frame_path": str(frame_path) if frame_path.is_file() else "",
                "time_reference_error": time_reference_error,
                "cut_mode": body.cut_mode.value,
                "total_windows": total_windows,
                "warnings_json": json.dumps(warnings, ensure_ascii=False),
            }
        )
        request.app.state.orchestrator.notify()
        return _public_job(created_job)

    @application.get("/api/jobs")
    async def list_jobs(
        request: Request,
        status: str | None = Query(default=None),
        limit: int = Query(default=200, ge=1, le=500),
    ):
        if status and status not in {item.value for item in JobStatus}:
            raise HTTPException(status_code=400, detail="Unknown job status")
        jobs = await request.app.state.database.list_jobs(status=status, limit=limit)
        return [_public_job(job) for job in jobs]

    @application.get("/api/jobs/{job_id}")
    async def get_job(request: Request, job_id: str):
        job = await request.app.state.database.get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        windows = await request.app.state.database.get_windows(job_id)
        attempts = await request.app.state.database.get_attempts_for_job(job_id)
        return {"job": _public_job(job), "windows": windows, "attempts": attempts}

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
            row["keywords"] = json.loads(row.pop("keywords_json"))
            row["raw"] = json.loads(row.pop("raw_json"))
        return rows

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
