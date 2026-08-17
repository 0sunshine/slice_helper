from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import secrets
import shutil
import sqlite3
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import __version__
from .archive_status import ArchiveCatalogReader, ArchivePreviewError
from .config import Settings
from .database import Database
from .excel_export import build_channel_workbook, safe_export_filename
from .islice import ISliceClient, ISlicePool
from .media import MediaError, MediaService
from .models import (
    CONTENT_TYPES,
    ChannelCreate,
    ChannelUpdate,
    JobReviewUpdate,
    TaskReviewUpdate,
    JobCreate,
    JobStatus,
    ISliceInstanceUpsert,
    ISliceMigrationRequest,
    SchedulingPriorityUpdate,
    SystemResetExecute,
    TimeReferenceRefresh,
    SegmentUpdate,
    SegmentMergeCreate,
    SegmentMergePreviewRequest,
    SegmentMergeUpdate,
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
from .system_reset import (
    SystemResetError,
    commit_agent_command,
    create_helper_backup,
    prepare_agent_command,
    validate_agent_receipts,
)
from .processing import calculate_total_windows
from .source_download import HttpSourceDownloader, SourceDownloadError
from .service_manager import ServiceManagementError, ServiceManager
from .time_ocr import TimeReference


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

PACKAGE_DIR = Path(__file__).resolve().parent
OCR_SAMPLE_COUNT = 10


def _random_ocr_frame_offsets(
    duration_seconds: float,
    *,
    sample_count: int = OCR_SAMPLE_COUNT,
    rng: random.Random | None = None,
) -> tuple[float, ...]:
    """Pick one random whole-second offset from each equal-duration region."""
    if sample_count < 1:
        raise ValueError("OCR sample count must be positive")
    generator = rng or random.SystemRandom()
    max_offset = max(0, int(duration_seconds) - 1)
    span = max_offset + 1
    offsets: list[float] = []
    for index in range(sample_count):
        region_start = index * span // sample_count
        region_end = ((index + 1) * span // sample_count) - 1
        if region_end < region_start:
            region_start = min(region_start, max_offset)
            region_end = region_start
        offsets.append(float(generator.randint(region_start, region_end)))
    generator.shuffle(offsets)
    return tuple(offsets)


async def _detect_random_time_reference(
    media: MediaService,
    source: Path,
    frame_path: Path,
    duration_seconds: float,
) -> tuple[TimeReference | None, str, tuple[float, ...]]:
    errors: list[str] = []
    attempted_offsets: list[float] = []
    for frame_offset in _random_ocr_frame_offsets(duration_seconds):
        attempted_offsets.append(frame_offset)
        try:
            reference = await media.detect_time_reference(
                source,
                frame_path,
                frame_offset_seconds=frame_offset,
            )
            return reference, "", tuple(attempted_offsets)
        except MediaError as exc:
            errors.append(f"{int(frame_offset)}s: {exc}")
    return None, "; ".join(errors), tuple(attempted_offsets)


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


def _public_segment(row: dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    result.setdefault("record_kind", "segment")
    result["accepted"] = bool(result.get("accepted"))
    result["ignored"] = bool(result.get("ignored"))
    result["manual_merge"] = bool(result.get("manual_merge"))
    result["keywords"] = json.loads(result.pop("keywords_json", "[]") or "[]")
    result["raw"] = json.loads(result.pop("raw_json", "{}") or "{}")
    return result


async def _resolve_archive_media(
    database: Database,
    archive_catalog: ArchiveCatalogReader,
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Resolve segment media through the archive catalog, with iSlice fallback."""
    instances = await database.list_islice_instances()
    by_url = {
        str(item.get("base_url") or "").rstrip("/"): item for item in instances
    }
    task_rows: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        task_id = str(row.get("task_id") or "")
        base_url = str(row.get("islice_base_url") or "").rstrip("/")
        if task_id and base_url:
            task_rows.setdefault((base_url, task_id), []).append(row)
    resolved: dict[tuple[str, str], dict[int, dict[str, Any]]] = {}
    errors: dict[tuple[str, str], str] = {}
    for key in task_rows:
        base_url, task_id = key
        instance = by_url.get(base_url)
        if not instance:
            errors[key] = "未找到对应的 iSlice 归档配置"
            continue
        try:
            preview = await archive_catalog.read_task_preview(instance, task_id)
            resolved[key] = {
                int(item.get("sourceIndex")): item
                for item in preview.get("segments") or []
                if isinstance(item, dict) and item.get("sourceIndex") is not None
            }
            for row in task_rows[key]:
                row["archive_status"] = "ready"
                row["archive_revision_digest"] = preview.get("revisionDigest") or ""
                row["archive_url"] = preview.get("archiveUrl") or ""
        except ArchivePreviewError as exc:
            errors[key] = str(exc)
    for row in rows:
        key = (
            str(row.get("islice_base_url") or "").rstrip("/"),
            str(row.get("task_id") or ""),
        )
        source_index = int(row.get("source_index") or 0)
        archived = resolved.get(key, {}).get(source_index)
        if archived:
            row["segment_url"] = archived.get("segmentUrl") or row.get("segment_url") or ""
            row["cover_img_url"] = archived.get("coverImgUrl") or row.get("cover_img_url") or ""
            row["archive_status"] = "ready"
        elif key in errors:
            row["archive_status"] = "pending" if "不存在" in errors[key] or "尚未" in errors[key] else "error"
            row["archive_error"] = errors[key]
        else:
            row.setdefault("archive_status", "not_applicable")
    return rows


def create_app(settings: Settings | None = None) -> FastAPI:
    configured = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        configured.data_dir.mkdir(parents=True, exist_ok=True)
        configured.temp_dir.mkdir(parents=True, exist_ok=True)
        database = Database(configured.database_path)
        await database.initialize()
        await database.seed_islice_instances(configured.configured_islice_urls)
        media = MediaService(configured)
        source_downloader = HttpSourceDownloader()
        await database.assign_legacy_jobs_with_attempts(configured.islice_base_url)
        instances = await database.list_islice_instances()
        islice = ISlicePool(
            configured,
            urls=tuple(str(item["base_url"]) for item in instances),
        )
        archive_catalog = ArchiveCatalogReader()
        orchestrator = Orchestrator(configured, database, media, islice)
        service_manager = ServiceManager(
            database, configured.data_dir, configured.public_base_url, PACKAGE_DIR
        )
        app.state.settings = configured
        app.state.database = database
        app.state.media = media
        app.state.source_downloader = source_downloader
        app.state.islice = islice
        app.state.archive_catalog = archive_catalog
        app.state.orchestrator = orchestrator
        app.state.service_manager = service_manager
        app.state.system_reset_lock = asyncio.Lock()
        app.state.system_write_condition = asyncio.Condition()
        app.state.active_write_requests = 0
        app.state.system_reset_in_progress = False
        await orchestrator.start()
        await service_manager.start()
        try:
            yield
        finally:
            await service_manager.stop()
            await orchestrator.stop()
            await archive_catalog.close()
            await islice.close()

    application = FastAPI(
        title="TS Continuous Slice Helper",
        version=__version__,
        lifespan=lifespan,
    )

    @application.middleware("http")
    async def reject_writes_during_system_reset(request: Request, call_next):
        is_write = request.method in {"POST", "PUT", "PATCH", "DELETE"}
        if not is_write or request.url.path == "/api/system-reset/execute":
            return await call_next(request)
        condition = request.app.state.system_write_condition
        async with condition:
            if request.app.state.system_reset_in_progress:
                return JSONResponse(status_code=503, content={"detail": "系统正在执行重置"})
            request.app.state.active_write_requests += 1
        try:
            return await call_next(request)
        finally:
            async with condition:
                request.app.state.active_write_requests -= 1
                condition.notify_all()

    templates = Jinja2Templates(directory=str(PACKAGE_DIR / "templates"))
    application.mount("/static", StaticFiles(directory=str(PACKAGE_DIR / "static")), name="static")

    @application.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def index(request: Request):
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={"version": application.version},
        )

    @application.get("/backup", response_class=HTMLResponse, include_in_schema=False)
    async def backup_page(request: Request):
        return templates.TemplateResponse(
            request=request,
            name="backup.html",
            context={"version": application.version},
        )

    @application.get("/task-review", response_class=HTMLResponse, include_in_schema=False)
    async def task_review_page(request: Request):
        return templates.TemplateResponse(
            request=request,
            name="task_review.html",
            context={"version": application.version, "content_types": CONTENT_TYPES},
        )

    @application.get("/api/islice-instances")
    async def list_islice_instances(request: Request):
        return await request.app.state.database.list_islice_instances()

    @application.get("/api/settings/scheduling-priority")
    async def get_scheduling_priority(request: Request):
        priority = await request.app.state.database.get_scheduling_priority()
        return {"priority": priority}

    @application.put("/api/settings/scheduling-priority")
    async def update_scheduling_priority(request: Request, body: SchedulingPriorityUpdate):
        priority = await request.app.state.database.set_scheduling_priority(body.priority)
        request.app.state.orchestrator.set_scheduling_priority(priority)
        request.app.state.orchestrator.notify()
        return {"priority": priority}

    @application.post("/api/islice-instances", status_code=201)
    async def create_islice_instance(request: Request, body: ISliceInstanceUpsert):
        record = body.model_dump()
        password = record.pop("ssh_password", None)
        record["ssh_password_encrypted"] = (
            request.app.state.service_manager.cipher.encrypt(password) if password else ""
        )
        try:
            instance = await request.app.state.database.create_islice_instance(
                record
            )
        except sqlite3.IntegrityError as exc:
            raise HTTPException(status_code=409, detail="sourceId 或 iSlice 地址已存在") from exc
        await request.app.state.islice.reconcile(
            await request.app.state.database.all_islice_urls()
        )
        request.app.state.orchestrator.notify()
        return instance

    @application.put("/api/islice-instances/{instance_id}")
    async def update_islice_instance(
        request: Request, instance_id: str, body: ISliceInstanceUpsert
    ):
        record = body.model_dump()
        password = record.pop("ssh_password", None)
        if password:
            record["ssh_password_encrypted"] = (
                request.app.state.service_manager.cipher.encrypt(password)
            )
        try:
            instance = await request.app.state.database.update_islice_instance(
                instance_id, record
            )
        except sqlite3.IntegrityError as exc:
            raise HTTPException(status_code=409, detail="sourceId 或 iSlice 地址已存在") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if instance is None:
            raise HTTPException(status_code=404, detail="iSlice 实例不存在")
        await request.app.state.islice.reconcile(
            await request.app.state.database.all_islice_urls()
        )
        request.app.state.orchestrator.notify()
        return instance

    @application.post("/api/islice-instances/{instance_id}/migration/validate")
    async def validate_islice_migration(
        request: Request, instance_id: str, body: ISliceMigrationRequest
    ):
        instance = await request.app.state.database.get_islice_instance(instance_id)
        if instance is None:
            raise HTTPException(status_code=404, detail="iSlice 实例不存在")
        old_url = str(instance["base_url"]).rstrip("/")
        client = ISliceClient(request.app.state.settings, base_url=body.base_url)
        try:
            online, message = await client.ping()
            return {
                "ready": bool(online),
                "oldBaseUrl": old_url,
                "newBaseUrl": body.base_url,
                "sourceId": instance["source_id"],
                "taskCount": 0,
                "foundCount": 0,
                "missingCount": 0,
                "missingTaskIds": [],
                "activeBlocker": False,
                "newNode": {"online": online, "message": message},
                "oldNode": {"online": False, "message": "旧节点不可访问，按迁移模式跳过"},
            }
        finally:
            await client.close()

    @application.post("/api/islice-instances/{instance_id}/migration/execute")
    async def execute_islice_migration(
        request: Request, instance_id: str, body: ISliceMigrationRequest
    ):
        validation = await validate_islice_migration(request, instance_id, body)
        if not validation["ready"]:
            raise HTTPException(status_code=409, detail={"code": "migration_not_ready", "validation": validation})
        record = body.model_dump()
        password = record.pop("ssh_password", None)
        if password:
            record["ssh_password_encrypted"] = request.app.state.service_manager.cipher.encrypt(password)
        updated = await request.app.state.database.migrate_islice_instance(instance_id, record, validation)
        if updated is None:
            raise HTTPException(status_code=404, detail="iSlice 实例不存在")
        await request.app.state.islice.reconcile(await request.app.state.database.all_islice_urls())
        request.app.state.orchestrator.notify()
        return {"status": "completed", "validation": validation, "instance": updated}

    @application.post("/api/islice-instances/{instance_id}/migration/rollback/{migration_id}")
    async def rollback_islice_migration(request: Request, instance_id: str, migration_id: str):
        try:
            result = await request.app.state.database.rollback_islice_migration(instance_id, migration_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if result is None:
            raise HTTPException(status_code=404, detail="迁移记录不存在或已回滚")
        await request.app.state.islice.reconcile(await request.app.state.database.all_islice_urls())
        request.app.state.orchestrator.notify()
        return {"status": "rolled_back", "instance": result}

    @application.post("/api/islice-instances/{instance_id}/deploy-agent")
    async def deploy_archive_agent(request: Request, instance_id: str):
        try:
            result = await request.app.state.service_manager.deploy(instance_id)
        except ServiceManagementError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return result

    @application.post("/api/islice-instances/{instance_id}/check-agent")
    async def check_archive_agent(request: Request, instance_id: str):
        instance = await request.app.state.database.get_islice_instance(instance_id)
        if instance is None:
            raise HTTPException(status_code=404, detail="服务不存在")
        return await request.app.state.service_manager.probe(instance_id)

    @application.delete("/api/islice-instances/{instance_id}", status_code=204)
    async def delete_islice_instance(request: Request, instance_id: str):
        try:
            deleted = await request.app.state.database.delete_islice_instance(instance_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if not deleted:
            raise HTTPException(status_code=404, detail="iSlice 实例不存在")
        await request.app.state.islice.reconcile(
            await request.app.state.database.all_islice_urls()
        )
        request.app.state.orchestrator.notify()
        return Response(status_code=204)

    @application.get("/api/archive/status")
    async def archive_status(
        request: Request,
        source_id: str | None = Query(default=None, alias="sourceId"),
        state: str | None = Query(default=None),
        channel_id: str | None = Query(
            default=None, alias="channelId", min_length=1, max_length=64
        ),
        broadcast_date: str | None = Query(
            default=None, alias="broadcastDate", min_length=10, max_length=10
        ),
        query: str = Query(default="", max_length=200),
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=20, alias="pageSize", ge=1, le=100),
    ):
        instances = await request.app.state.database.list_islice_instances()
        contexts = await request.app.state.database.archive_task_contexts()
        catalog = await request.app.state.archive_catalog.read(instances, contexts)
        if broadcast_date:
            try:
                date.fromisoformat(broadcast_date)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail="业务日期格式必须为 YYYY-MM-DD") from exc
        needle = query.strip().casefold()
        tasks = []
        for task in catalog["tasks"]:
            if source_id and task.get("source_id") != source_id:
                continue
            if state and task.get("state") != state:
                continue
            context = task.get("context") or {}
            if channel_id and str(context.get("channel_id") or "") != channel_id:
                continue
            if broadcast_date and str(context.get("broadcast_date") or "") != broadcast_date:
                continue
            haystack = " ".join(
                str(value or "")
                for value in (
                    task.get("task_id"),
                    task.get("source_name"),
                    task.get("error_message"),
                    context.get("job_id"),
                    context.get("channel_name"),
                    context.get("broadcast_date"),
                )
            ).casefold()
            if needle and needle not in haystack:
                continue
            tasks.append(task)
        tasks.sort(
            key=lambda item: (
                str((item.get("context") or {}).get("broadcast_date") or ""),
                float((item.get("context") or {}).get("requested_start") or -1),
                str(
                    item.get("archived_at")
                    or item.get("updated_at")
                    or item.get("discovered_at")
                    or ""
                ),
                str(item.get("task_id") or ""),
            ),
            reverse=True,
        )
        total = len(tasks)
        total_pages = max(1, (total + page_size - 1) // page_size)
        page = min(page, total_pages)
        start = (page - 1) * page_size
        return {
            "sources": catalog["sources"],
            "items": tasks[start : start + page_size],
            "page": page,
            "pageSize": page_size,
            "total": total,
            "totalPages": total_pages,
        }

    @application.get("/api/archive/tasks/{source_id}/{task_id}/preview")
    async def archive_task_preview(
        request: Request,
        source_id: str,
        task_id: str,
        revision_digest: str | None = Query(
            default=None, alias="revisionDigest", pattern=r"^[0-9a-f]{64}$"
        ),
    ):
        instances = await request.app.state.database.list_islice_instances()
        instance = next(
            (item for item in instances if str(item["source_id"]) == source_id),
            None,
        )
        if instance is None:
            raise HTTPException(status_code=404, detail="归档来源不存在")
        try:
            return await request.app.state.archive_catalog.read_task_preview(
                instance,
                task_id,
                revision_digest,
            )
        except ArchivePreviewError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @application.get("/api/islice-instances/{instance_id}/archive-readiness")
    async def archive_readiness(request: Request, instance_id: str):
        instance = await request.app.state.database.get_islice_instance(instance_id)
        if instance is None:
            raise HTTPException(status_code=404, detail="iSlice 实例不存在")
        base_url = str(instance.get("base_url") or "").rstrip("/")
        jobs = await request.app.state.database.list_jobs(limit=100000)
        scoped = [
            job for job in jobs
            if str(job.get("islice_base_url") or "").rstrip("/") == base_url
            and not job.get("superseded_at")
        ]
        task_ids: set[str] = set()
        for job in scoped:
            attempts = await request.app.state.database.get_attempts_for_job(str(job["id"]))
            task_ids.update(
                str(row.get("task_id") or "")
                for row in attempts
                if row.get("task_id")
                and (
                    str(row.get("status") or "") == "completed"
                    or str(row.get("service_status") or "") == "completed"
                )
            )
        items: list[dict[str, Any]] = []
        for task_id in sorted(task_ids):
            try:
                preview = await request.app.state.archive_catalog.read_task_preview(instance, task_id)
                media = preview.get("segments") or []
                missing = sum(1 for row in media if not row.get("segmentUrl"))
                items.append({"taskId": task_id, "status": "ready" if not missing else "missing_media", "missingMedia": missing})
            except ArchivePreviewError as exc:
                items.append({"taskId": task_id, "status": "pending", "error": str(exc)})
        failed = [item for item in items if item["status"] != "ready"]
        return {
            "instanceId": instance_id,
            "baseUrl": base_url,
            "jobCount": len(scoped),
            "taskCount": len(items),
            "ready": not failed,
            "items": items,
        }

    @application.post("/api/system-reset/preview")
    async def preview_system_reset(request: Request):
        database = request.app.state.database
        preview_state = await database.reset_preview_counts()
        if preview_state["active_jobs"]:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "active_jobs",
                    "message": "仍有活动或待调度作业，必须先暂停或停止",
                    "jobs": preview_state["active_jobs"],
                },
            )
        instances = await database.list_islice_instances()
        if not instances:
            raise HTTPException(status_code=409, detail="没有配置 iSlice 实例")
        request_id = uuid.uuid4().hex
        nonce = secrets.token_urlsafe(24)
        short_id = request_id[:8]
        confirmation_text = f"RESET {short_id}"
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=15)
        sources = [
            {
                "sourceId": str(instance["source_id"]),
                "name": str(instance["name"]),
                "baseUrl": str(instance["base_url"]),
                "agentInstallPath": str(instance.get("agent_install_path") or ""),
                "prepareConfirmation": f"BACKUP {instance['source_id']} {short_id}",
            }
            for instance in instances
        ]
        preview = {
            "requestId": request_id,
            "counts": preview_state["counts"],
            "sources": sources,
            "mediaDirectoriesIncluded": False,
        }
        await database.create_system_reset_request(
            request_id=request_id,
            nonce=nonce,
            confirmation_hash=hashlib.sha256(
                confirmation_text.encode("utf-8")
            ).hexdigest(),
            expires_at=expires_at.isoformat(),
            preview=preview,
        )
        for source in sources:
            source["prepareCommand"] = prepare_agent_command(
                source["sourceId"],
                request_id,
                nonce,
                source["prepareConfirmation"],
                source["agentInstallPath"],
            )
            # Keep a generic command key for clients that render prepare and
            # commit commands through the same component.  The explicit
            # prepareCommand key remains part of the response for clarity.
            source["command"] = source["prepareCommand"]
        return {
            **preview,
            "expiresAt": expires_at.isoformat(),
            "confirmationText": confirmation_text,
            "warnings": [
                "只备份并重置数据库，不备份、不删除任何媒体目录",
                "执行后所有 iSlice 实例会自动关闭新作业调度",
                "必须先在每台 iSlice 上执行 prepare-reset 并粘贴全部 JSON 回执",
            ],
        }

    @application.post("/api/system-reset/execute")
    async def execute_system_reset(request: Request, body: SystemResetExecute):
        if not body.acknowledge_media_handling:
            raise HTTPException(status_code=400, detail="必须确认媒体目录由用户自行处理")
        lock = request.app.state.system_reset_lock
        async with lock:
            condition = request.app.state.system_write_condition
            async with condition:
                request.app.state.system_reset_in_progress = True
                while request.app.state.active_write_requests:
                    await condition.wait()
            try:
                database = request.app.state.database
                reset_request = await database.get_system_reset_request(body.request_id)
                if reset_request is None:
                    raise HTTPException(status_code=404, detail="重置请求不存在")
                if reset_request["status"] != "prepared":
                    raise HTTPException(status_code=409, detail="重置请求已使用或已失效")
                expires_at = datetime.fromisoformat(str(reset_request["expires_at"]))
                if expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=timezone.utc)
                if expires_at <= datetime.now(timezone.utc):
                    raise HTTPException(status_code=409, detail="重置请求已过期")
                expected_hash = str(reset_request["confirmation_hash"])
                supplied_hash = hashlib.sha256(
                    body.confirmation_text.encode("utf-8")
                ).hexdigest()
                if not secrets.compare_digest(expected_hash, supplied_hash):
                    raise HTTPException(status_code=400, detail="二次确认短语不正确")
                preview = json.loads(str(reset_request["preview_json"]))
                required_sources = {
                    str(source["sourceId"]) for source in preview.get("sources", [])
                }
                try:
                    receipts = validate_agent_receipts(
                        [dict(item) for item in body.receipts],
                        request_id=body.request_id,
                        nonce=str(reset_request["nonce"]),
                        required_source_ids=required_sources,
                    )
                    helper_backup = await asyncio.to_thread(
                        create_helper_backup,
                        configured.database_path,
                        configured.data_dir,
                        body.request_id,
                    )
                    deleted_counts = await database.reset_operational_data(
                        request_id=body.request_id,
                        receipts=receipts,
                        helper_backup_path=str(helper_backup["databaseBackup"]),
                    )
                except (SystemResetError, ValueError, OSError, sqlite3.Error) as exc:
                    raise HTTPException(status_code=409, detail=str(exc)) from exc
                commands = []
                install_paths = {
                    str(source["sourceId"]): str(source.get("agentInstallPath") or "")
                    for source in preview.get("sources", [])
                }
                for receipt in receipts:
                    confirmation = (
                        f"RESET {receipt['sourceId']} {body.request_id[:8]}"
                    )
                    commands.append(
                        {
                            "sourceId": receipt["sourceId"],
                            "confirmation": confirmation,
                            "command": commit_agent_command(
                                receipt,
                                confirmation,
                                install_paths.get(str(receipt["sourceId"]), ""),
                            ),
                        }
                    )
                request.app.state.orchestrator.notify()
                return {
                    "status": "helper_reset",
                    "requestId": body.request_id,
                    "helperBackup": helper_backup,
                    "deletedCounts": deleted_counts,
                    "commitCommands": commands,
                    "mediaDirectoriesTouched": False,
                    "message": "helper 已重置；请在每台 iSlice 停止服务后执行 commit-reset",
                }
            finally:
                async with condition:
                    request.app.state.system_reset_in_progress = False
                    condition.notify_all()

    @application.get("/internal/archive-references/{task_id}")
    async def archive_references(
        request: Request,
        task_id: str,
        islice_base_url: str | None = Query(default=None, alias="isliceBaseUrl"),
    ):
        rows = await request.app.state.database.archive_references(task_id)
        if islice_base_url:
            normalized = islice_base_url.rstrip("/")
            rows = [
                row
                for row in rows
                if str(row.get("islice_base_url") or "").rstrip("/") == normalized
            ]
        media_paths: set[str] = set()
        for row in rows:
            for field in ("segment_url", "cover_img_url"):
                parts = urlsplit(str(row.get(field) or "")).path.strip("/").split("/")
                try:
                    index = parts.index("download")
                    url_task, directory, filename = parts[index + 1 : index + 4]
                except (ValueError, IndexError):
                    continue
                if url_task == task_id and directory in {"segments", "covers"} and filename:
                    media_paths.add(f"{directory}/{filename}")
        return {
            "taskId": task_id,
            "found": bool(rows),
            "isliceBaseUrls": sorted(
                {str(row["islice_base_url"]) for row in rows if row.get("islice_base_url")}
            ),
            "mediaPaths": sorted(media_paths),
            "references": len(rows),
        }

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
        if request.app.state.system_reset_in_progress:
            raise HTTPException(status_code=503, detail="系统正在执行重置")
        channel = await request.app.state.database.get_channel(body.channel_id)
        if channel is None:
            raise HTTPException(status_code=400, detail="请选择有效频道")
        selected_islice_url = ""
        if body.islice_base_url:
            selected_islice_url = body.islice_base_url.rstrip("/")
            instances = await request.app.state.database.list_islice_instances()
            selected = next((item for item in instances if str(item.get("base_url") or "").rstrip("/") == selected_islice_url), None)
            if selected is None:
                raise HTTPException(status_code=400, detail="所选 iSlice 实例不存在")
            if not selected.get("schedulable"):
                raise HTTPException(status_code=400, detail="所选 iSlice 实例当前不可调度")
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
        time_reference, time_reference_error, attempted_offsets = (
            await _detect_random_time_reference(
                request.app.state.media,
                source,
                frame_path,
                probe.duration,
            )
        )

        if time_reference is not None:
            resolved_start_time = time_reference.source_start_time
            time_reference_source = "ocr"
            time_reference_error = ""
            if time_reference.frame_offset_seconds:
                warnings.append(
                    "Time OCR succeeded at source offset "
                    f"{int(time_reference.frame_offset_seconds)}s after "
                    f"{len(attempted_offsets)} attempts"
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
            warnings.append(
                f"Time OCR failed after {len(attempted_offsets)} attempts: "
                f"{time_reference_error}"
            )

        stopped_for_missing_time = resolved_start_time is None
        missing_time_message = (
            f"Time OCR failed after {len(attempted_offsets)} attempts and "
            "programStartTime was not supplied; "
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
                "islice_base_url": selected_islice_url,
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
        islice_base_url: str | None = Query(default=None, alias="isliceBaseUrl"),
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
            islice_base_url=islice_base_url.rstrip("/") if islice_base_url else None,
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
                islice_base_url=islice_base_url.rstrip("/") if islice_base_url else None,
            )
        return {
            "items": [_public_job(job) for job in jobs],
            "page": page,
            "pageSize": page_size,
            "total": total,
            "totalPages": total_pages,
        }

    @application.get("/api/task-reviews")
    async def list_task_reviews(
        request: Request,
        channel_id: str | None = Query(default=None, alias="channelId"),
        broadcast_date: str | None = Query(default=None, alias="broadcastDate"),
        islice_base_url: str | None = Query(default=None, alias="isliceBaseUrl"),
        review_status: str | None = Query(default=None, alias="reviewStatus"),
        content_type: str | None = Query(default=None, alias="contentType"),
        query: str | None = Query(default=None, max_length=200),
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=20, alias="pageSize", ge=1, le=100),
    ):
        if review_status and review_status not in {
            "unreviewed", "hold", "approved", "rejected"
        }:
            raise HTTPException(status_code=400, detail="Unknown task review status")
        if content_type and content_type not in CONTENT_TYPES:
            raise HTTPException(status_code=400, detail="Unknown content type")
        if broadcast_date:
            try:
                date.fromisoformat(broadcast_date)
            except ValueError as exc:
                raise HTTPException(
                    status_code=400, detail="业务日期格式应为 YYYY-MM-DD"
                ) from exc
        items, total = await request.app.state.database.paginate_completed_tasks(
            page=page,
            page_size=page_size,
            channel_id=channel_id,
            broadcast_date=broadcast_date,
            islice_base_url=(islice_base_url.rstrip("/") if islice_base_url else None),
            review_status=review_status,
            content_type=content_type,
            query=query,
        )
        total_pages = max(1, (total + page_size - 1) // page_size)
        if page > total_pages:
            page = total_pages
            items, _ = await request.app.state.database.paginate_completed_tasks(
                page=page,
                page_size=page_size,
                channel_id=channel_id,
                broadcast_date=broadcast_date,
                islice_base_url=(islice_base_url.rstrip("/") if islice_base_url else None),
                review_status=review_status,
                content_type=content_type,
                query=query,
            )
        return {
            "items": items,
            "page": page,
            "pageSize": page_size,
            "total": total,
            "totalPages": total_pages,
        }

    @application.get("/api/task-reviews/{attempt_id}/segments")
    async def get_task_review_segments(request: Request, attempt_id: int):
        task = await request.app.state.database.get_completed_task(attempt_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Completed task not found")
        rows = await request.app.state.database.get_segments_for_attempt(attempt_id)
        rows = await _resolve_archive_media(
            request.app.state.database, request.app.state.archive_catalog, rows
        )
        return {
            "task": task,
            "segments": [_public_segment(row) for row in rows],
        }

    @application.patch("/api/task-reviews/{attempt_id}")
    async def update_task_review(
        request: Request, attempt_id: int, body: TaskReviewUpdate
    ):
        fields = body.model_dump(exclude_unset=True)
        if "ai_review_comment" in fields:
            fields["ai_review_comment"] = fields["ai_review_comment"] or ""
        updated = await request.app.state.database.update_task_review(attempt_id, **fields)
        if updated is None:
            raise HTTPException(status_code=404, detail="Completed task not found")
        return updated

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
        rows = await _resolve_archive_media(
            request.app.state.database, request.app.state.archive_catalog, rows
        )
        return [_public_segment(row) for row in rows]

    @application.get("/api/jobs/{job_id}/archive-status")
    async def job_archive_status(request: Request, job_id: str):
        job = await request.app.state.database.get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        rows = await request.app.state.database.get_segments(job_id, accepted_only=False)
        rows = await _resolve_archive_media(
            request.app.state.database, request.app.state.archive_catalog, rows
        )
        by_task: dict[str, dict[str, Any]] = {}
        for row in rows:
            task_id = str(row.get("task_id") or "")
            if not task_id:
                continue
            item = by_task.setdefault(
                task_id,
                {"taskId": task_id, "status": "ready", "segments": 0, "errors": []},
            )
            item["segments"] += 1
            status = str(row.get("archive_status") or "pending")
            if status != "ready":
                item["status"] = "error" if status == "error" else "pending"
                if row.get("archive_error") and row["archive_error"] not in item["errors"]:
                    item["errors"].append(row["archive_error"])
        items = list(by_task.values())
        return {
            "jobId": job_id,
            "status": "ready" if items and all(item["status"] == "ready" for item in items) else ("pending" if items else "not_applicable"),
            "tasks": items,
        }

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
        try:
            updated = await request.app.state.database.update_segment(
                job_id, segment_id, **fields
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if updated is None:
            raise HTTPException(status_code=404, detail="Segment not found")
        await request.app.state.orchestrator.write_manifest(job_id)
        return _public_segment(updated)

    @application.post("/api/jobs/{job_id}/segment-merges/preview")
    async def preview_segment_merge(
        request: Request, job_id: str, body: SegmentMergePreviewRequest
    ):
        if not await request.app.state.database.get_job(job_id):
            raise HTTPException(status_code=404, detail="Job not found")
        try:
            preview = await request.app.state.database.preview_segment_merge(
                job_id, body.segment_ids, body.primary_segment_id
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {
            "jobId": job_id,
            "segmentIds": [int(row["id"]) for row in preview["segments"]],
            "primarySegmentId": int(preview["primary"]["id"]),
            "members": [
                {
                    "id": int(row["id"]),
                    "windowIndex": int(row["window_index"]),
                    "title": row["title"],
                    "contentType": row["content_type"],
                    "newsEventType": row["news_event_type"],
                    "globalStart": float(row["global_start"]),
                    "globalEnd": float(row["global_end"]),
                    "absoluteStart": row["absolute_start"],
                    "absoluteEnd": row["absolute_end"],
                    "primary": int(row["id"]) == body.primary_segment_id,
                }
                for row in preview["segments"]
            ],
            "result": {
                "title": preview["primary"]["title"],
                "contentType": preview["primary"]["content_type"],
                "newsEventType": preview["primary"]["news_event_type"],
                "globalStart": preview["global_start"],
                "globalEnd": preview["global_end"],
                "absoluteStart": min(
                    row["absolute_start"]
                    for row in preview["segments"]
                    if row["absolute_start"] is not None
                ) if any(row["absolute_start"] for row in preview["segments"]) else None,
                "absoluteEnd": max(
                    row["absolute_end"]
                    for row in preview["segments"]
                    if row["absolute_end"] is not None
                ) if any(row["absolute_end"] for row in preview["segments"]) else None,
            },
            "gapSeconds": preview["gap_seconds"],
            "overlapSeconds": preview["overlap_seconds"],
            "previewToken": preview["preview_token"],
        }

    @application.post("/api/jobs/{job_id}/segment-merges", status_code=201)
    async def create_segment_merge(
        request: Request, job_id: str, body: SegmentMergeCreate
    ):
        if not await request.app.state.database.get_job(job_id):
            raise HTTPException(status_code=404, detail="Job not found")
        try:
            merge = await request.app.state.database.create_segment_merge(
                job_id,
                body.segment_ids,
                body.primary_segment_id,
                body.preview_token,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        await request.app.state.orchestrator.write_manifest(job_id)
        return {
            "id": merge["id"],
            "status": merge["status"],
            "memberCount": int(merge["member_count"]),
        }

    @application.patch("/api/jobs/{job_id}/segment-merges/{merge_id}")
    async def update_segment_merge(
        request: Request,
        job_id: str,
        merge_id: str,
        body: SegmentMergeUpdate,
    ):
        fields: dict[str, Any] = {}
        if "title" in body.model_fields_set:
            fields["title"] = body.title or ""
        if "content_type" in body.model_fields_set:
            fields["content_type"] = body.content_type or ""
        if "ignored" in body.model_fields_set:
            fields["ignored"] = int(bool(body.ignored))
        updated = await request.app.state.database.update_segment_merge(
            job_id, merge_id, **fields
        )
        if updated is None:
            raise HTTPException(status_code=404, detail="Active manual merge not found")
        await request.app.state.orchestrator.write_manifest(job_id)
        segments = await request.app.state.database.get_segments(job_id, accepted_only=False)
        row = next(
            (item for item in segments if item.get("merge_id") == merge_id), None
        )
        if row is None:
            raise HTTPException(status_code=404, detail="Active manual merge not found")
        return _public_segment(row)

    @application.delete("/api/jobs/{job_id}/segment-merges/{merge_id}")
    async def cancel_segment_merge(request: Request, job_id: str, merge_id: str):
        cancelled = await request.app.state.database.cancel_segment_merge(
            job_id, merge_id
        )
        if not cancelled:
            raise HTTPException(status_code=404, detail="Active manual merge not found")
        await request.app.state.orchestrator.write_manifest(job_id)
        return Response(status_code=204)

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

    @application.post("/api/jobs/{job_id}/refresh-time-reference")
    async def refresh_job_time_reference(
        request: Request, job_id: str, body: TimeReferenceRefresh | None = None
    ):
        job = await _require_job(request, job_id)
        if body and body.program_start_time is not None:
            updated, segment_count = await request.app.state.database.update_time_reference(
                job_id, body.program_start_time.isoformat(), source="manual_override",
                reference_text=body.program_start_time.isoformat(), reference_error="",
                warning=f"Time reference manually supplied/refreshed by operator: {body.program_start_time.isoformat()}",
            )
            if updated is None:
                raise HTTPException(status_code=404, detail="Job not found")
            await request.app.state.orchestrator.write_manifest(job_id)
            request.app.state.orchestrator.notify()
            return {"job": _public_job(updated), "updatedSegmentCount": segment_count, "attemptCount": 0, "manual": True}
        source = Path(job["source_path"])
        try:
            stat = source.stat()
        except OSError as exc:
            raise HTTPException(status_code=409, detail="原始 TS 文件不存在或无法读取") from exc
        if (
            stat.st_size != int(job["source_size"])
            or stat.st_mtime_ns != int(job["source_mtime_ns"])
        ):
            raise HTTPException(status_code=409, detail="原始 TS 文件已经发生变化，无法重新识别")

        job_dir = configured.data_dir / "jobs" / job_id
        frame_path = job_dir / "time-reference.png"
        refresh_frame_path = job_dir / "time-reference-refresh.png"
        refresh_frame_path.unlink(missing_ok=True)
        try:
            reference, error, attempted_offsets = await _detect_random_time_reference(
                request.app.state.media,
                source,
                refresh_frame_path,
                float(job["source_duration"]),
            )
            if reference is None:
                await request.app.state.database.update_job(
                    job_id, time_reference_error=error
                )
                await request.app.state.orchestrator.write_manifest(job_id)
                raise HTTPException(
                    status_code=422,
                    detail={
                        "code": "time_ocr_failed",
                        "message": f"全片随机 OCR {len(attempted_offsets)} 次均未识别到时间",
                    },
                )

            refresh_frame_path.replace(frame_path)
            resolved_start_time = reference.source_start_time.isoformat()
            updated, segment_count = (
                await request.app.state.database.update_time_reference(
                    job_id,
                    resolved_start_time,
                    source="ocr",
                    reference_text=reference.matched_text,
                    reference_confidence=reference.confidence,
                    reference_frame_path=str(frame_path),
                    reference_frame_offset=reference.frame_offset_seconds,
                    reference_error="",
                    warning=(
                        "Time reference refreshed by OCR at source offset "
                        f"{int(reference.frame_offset_seconds)}s after "
                        f"{len(attempted_offsets)} attempts: {resolved_start_time}"
                    ),
                )
            )
            if updated is None:
                raise HTTPException(status_code=404, detail="Job not found")
            await request.app.state.orchestrator.write_manifest(job_id)
            request.app.state.orchestrator.notify()
            return {
                "job": _public_job(updated),
                "updatedSegmentCount": segment_count,
                "attemptCount": len(attempted_offsets),
                "frameOffset": reference.frame_offset_seconds,
            }
        finally:
            refresh_frame_path.unlink(missing_ok=True)

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
