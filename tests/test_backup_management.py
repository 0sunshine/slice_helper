from __future__ import annotations

import asyncio
import shlex
import sqlite3
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from slice_helper.app import create_app
from slice_helper.archive_status import ArchiveCatalogReader, ArchivePreviewError
from slice_helper.config import Settings
from slice_helper.database import Database
from slice_helper.orchestrator import Orchestrator


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        islice_base_url="http://192.168.104.128:8000",
        public_base_url="http://helper.test",
        host="127.0.0.1",
        port=8090,
        data_dir=tmp_path / "data",
        temp_dir=tmp_path / "temp",
        ffmpeg_path="ffmpeg",
        ffprobe_path="ffprobe",
        poll_interval_seconds=0.01,
        window_timeout_seconds=1,
        ffmpeg_timeout_seconds=10,
    )


def job_record(tmp_path: Path, job_id: str, **overrides) -> dict:
    return {
        "id": job_id,
        "source_path": str(tmp_path / f"{job_id}.ts"),
        "source_size": 10,
        "source_mtime_ns": 20,
        "source_duration": 60.0,
        "template_id": "general",
        "language": "zh",
        "channel_name": "测试频道",
        "program_start_time": None,
        "cut_mode": "copy",
        "total_windows": 1,
        **overrides,
    }


@pytest.mark.asyncio
async def test_instance_registry_uses_stable_source_ids_and_disabled_routing(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "state.db")
    await database.initialize()
    await database.seed_islice_instances(
        ("http://192.168.104.128:8000", "http://192.168.104.129:8000")
    )
    instances = await database.list_islice_instances()
    assert [item["source_id"] for item in instances] == ["islice-128", "islice-129"]

    first = instances[0]
    await database.create_job(
        job_record(
            tmp_path,
            "bound-job",
            islice_base_url=first["base_url"],
        )
    )
    await database.create_job(job_record(tmp_path, "unbound-job"))
    updated = await database.update_islice_instance(
        first["id"],
        {
            "source_id": first["source_id"],
            "name": "主 iSlice",
            "base_url": first["base_url"],
            "archive_catalog_url": "http://archive.test/sources/islice-128/catalog.json",
            "schedulable": False,
        },
    )
    assert updated and not updated["schedulable"]

    claimed = await database.claim_schedulable_jobs(
        (), 10, (str(first["base_url"]),)
    )
    assert [item["id"] for item in claimed] == ["bound-job"]
    assert (await database.get_job("unbound-job"))["status"] == "pending_schedule"

    with pytest.raises(ValueError, match="sourceId"):
        await database.update_islice_instance(
            first["id"],
            {
                "source_id": "renamed-source",
                "name": "主 iSlice",
                "base_url": first["base_url"],
                "archive_catalog_url": "",
                "schedulable": False,
            },
        )
    with pytest.raises(ValueError, match="address"):
        await database.update_islice_instance(
            first["id"],
            {
                "source_id": first["source_id"],
                "name": "主 iSlice",
                "base_url": "http://192.168.104.130:8000",
                "archive_catalog_url": "",
                "schedulable": False,
            },
        )

    assert await database.delete_islice_instance(instances[1]["id"])
    with pytest.raises(ValueError, match="used"):
        await database.delete_islice_instance(first["id"])


@pytest.mark.asyncio
async def test_archive_references_are_scoped_to_islice_and_accepted_media(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "state.db")
    await database.initialize()
    await database.seed_islice_instances(("http://192.168.104.128:8000",))
    await database.create_job(
        job_record(
            tmp_path,
            "archive-job",
            islice_base_url="http://192.168.104.128:8000",
            status="completed",
        )
    )
    window = await database.upsert_window("archive-job", 0, 0, 60)
    await database.create_attempt(window["id"], 1, "shared-task-id")
    await database.replace_window_segments(
        "archive-job",
        window["id"],
        [
            {
                "source_index": 0,
                "accepted": 1,
                "reason": "",
                "local_start": 0.0,
                "local_end": 10.0,
                "global_start": 0.0,
                "global_end": 10.0,
                "title": "保留片段",
                "content_type": "新闻",
                "news_event_type": "",
                "topic": "",
                "keywords_json": "[]",
                "summary": "",
                "segment_url": "http://islice/download/shared-task-id/segments/a.mp4",
                "cover_img_url": "http://islice/download/shared-task-id/covers/a.jpg",
                "raw_json": "{}",
            }
        ],
    )
    rows = await database.archive_references("shared-task-id")
    assert len(rows) == 1
    assert rows[0]["islice_base_url"] == "http://192.168.104.128:8000"


@pytest.mark.asyncio
async def test_archive_catalog_errors_are_isolated_and_source_id_is_verified() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/good/catalog.json":
            return httpx.Response(
                200,
                json={
                    "source": {"id": "islice-good"},
                    "summary": {"taskCount": 1, "totalBytes": 123, "states": {}},
                    "tasks": [{"task_id": "task-1", "state": "deleted"}],
                },
            )
        if request.url.path == "/wrong/catalog.json":
            return httpx.Response(
                200,
                json={"source": {"id": "somebody-else"}, "tasks": []},
            )
        if request.url.path == "/missing/catalog.json":
            return httpx.Response(404)
        return httpx.Response(503)

    reader = ArchiveCatalogReader()
    await reader.client.aclose()
    reader.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    instances = [
        {
            "source_id": "islice-good",
            "name": "正常节点",
            "base_url": "http://islice-good.test",
            "archive_catalog_url": "http://archive.test/good/catalog.json",
            "schedulable": True,
        },
        {
            "source_id": "islice-wrong",
            "name": "配置错误节点",
            "base_url": "http://islice-wrong.test",
            "archive_catalog_url": "http://archive.test/wrong/catalog.json",
            "schedulable": False,
        },
        {
            "source_id": "islice-empty",
            "name": "未配置节点",
            "base_url": "http://islice-empty.test",
            "archive_catalog_url": "",
            "schedulable": False,
        },
        {
            "source_id": "islice-missing",
            "name": "Missing catalog",
            "base_url": "http://islice-missing.test",
            "archive_catalog_url": "http://archive.test/missing/catalog.json",
            "schedulable": False,
        },
    ]
    try:
        result = await reader.read(
            instances,
            {("islice-good", "task-1"): {"job_id": "job-1"}},
        )
    finally:
        await reader.close()
    assert [source["online"] for source in result["sources"]] == [True, False, False, False]
    assert "不匹配" in result["sources"][1]["error"]
    assert "未配置" in result["sources"][2]["error"]
    assert "尚未发布 catalog" in result["sources"][3]["error"]
    assert result["tasks"][0]["context"]["job_id"] == "job-1"


@pytest.mark.asyncio
async def test_archive_preview_rewrites_islice_media_to_archive_http() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/sources/islice-128/catalog.json":
            return httpx.Response(
                200,
                json={
                    "source": {"id": "islice-128"},
                    "tasks": [
                        {
                            "task_id": "task-001",
                            "manifest_digest": "a" * 64,
                            "archive_url": "http://archive.test/sources/islice-128/tasks/task-001",
                            "revisions": [],
                        }
                    ],
                },
            )
        if request.url.path == "/sources/islice-128/tasks/task-001/segments.json":
            return httpx.Response(
                200,
                json={
                    "segments": [
                        {
                            "startTime": 12.5,
                            "endTime": 42.0,
                            "title": "归档片段",
                            "segmentUrl": "http://islice/download/task-001/segments/one clip.mp4",
                            "coverImgUrl": "http://islice/download/task-001/covers/one.jpg",
                        }
                    ]
                },
            )
        return httpx.Response(404)

    reader = ArchiveCatalogReader()
    await reader.client.aclose()
    reader.client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=True
    )
    instance = {
        "source_id": "islice-128",
        "name": "iSlice 128",
        "archive_catalog_url": "http://archive.test/sources/islice-128/catalog.json",
    }
    try:
        preview = await reader.read_task_preview(instance, "task-001")
    finally:
        await reader.close()

    assert preview["taskId"] == "task-001"
    assert preview["revisionDigest"] == "a" * 64
    assert preview["segments"][0]["segmentUrl"] == (
        "http://archive.test/sources/islice-128/tasks/task-001/segments/one%20clip.mp4"
    )
    assert preview["segments"][0]["coverImgUrl"].endswith("/covers/one.jpg")


@pytest.mark.asyncio
async def test_archive_preview_rejects_catalog_url_outside_source_root() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "source": {"id": "islice-128"},
                "tasks": [
                    {
                        "task_id": "task-001",
                        "archive_url": "http://untrusted.test/task-001",
                        "revisions": [],
                    }
                ],
            },
        )

    reader = ArchiveCatalogReader()
    await reader.client.aclose()
    reader.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ArchivePreviewError, match="超出"):
            await reader.read_task_preview(
                {
                    "source_id": "islice-128",
                    "name": "iSlice 128",
                    "archive_catalog_url": "http://archive.test/sources/islice-128/catalog.json",
                },
                "task-001",
            )
    finally:
        await reader.close()


def test_backup_page_and_instance_api(tmp_path: Path, monkeypatch) -> None:
    async def no_start(_self):
        return None

    async def no_stop(_self):
        return None

    async def fake_catalog(_self, instances, _contexts):
        return {"sources": [], "tasks": []}

    async def fake_preview(_self, instance, task_id, revision_digest=None):
        return {
            "sourceId": instance["source_id"],
            "taskId": task_id,
            "revisionDigest": revision_digest or "",
            "segments": [],
        }

    monkeypatch.setattr(Orchestrator, "start", no_start)
    monkeypatch.setattr(Orchestrator, "stop", no_stop)
    monkeypatch.setattr(ArchiveCatalogReader, "read", fake_catalog)
    monkeypatch.setattr(ArchiveCatalogReader, "read_task_preview", fake_preview)

    with TestClient(create_app(make_settings(tmp_path))) as client:
        page = client.get("/backup")
        assert page.status_code == 200
        assert "iSlice 服务与备份" in page.text
        assert 'id="backupChannel"' in page.text
        assert 'id="backupDate"' in page.text
        assert 'id="archivePreviewDialog"' in page.text
        assert 'id="archivePreviewVideo"' in page.text
        assert 'id="instanceSshHost"' in page.text
        assert 'id="instanceAgentInstallPath"' in page.text
        assert "保存后自动部署并拉起归档代理" in page.text
        assert "添加服务" in page.text
        assert 'id="startSystemReset"' in page.text
        assert 'id="resetDialog"' in page.text
        assert "媒体目录不会被备份或删除" in page.text
        instances = client.get("/api/islice-instances").json()
        assert instances[0]["source_id"] == "islice-128"
        response = client.post(
            "/api/islice-instances",
            json={
                "sourceId": "islice-129",
                "name": "iSlice 129",
                "baseUrl": "http://192.168.104.129:8000",
                "archiveCatalogUrl": "http://archive.test/sources/islice-129/catalog.json",
                "schedulable": True,
            },
        )
        assert response.status_code == 201
        assert response.json()["has_ssh_password"] is False
        assert "ssh_password_encrypted" not in response.json()

        async def fake_deploy(instance_id):
            return {"online": True, "version": "test", "instanceId": instance_id}

        client.app.state.service_manager.deploy = fake_deploy
        deployed = client.post(
            f"/api/islice-instances/{response.json()['id']}/deploy-agent"
        )
        assert deployed.status_code == 200
        assert deployed.json()["online"] is True
        status = client.get("/api/archive/status").json()
        assert status["total"] == 0
        preview = client.get("/api/archive/tasks/islice-128/task-1/preview")
        assert preview.status_code == 200
        assert preview.json()["taskId"] == "task-1"
        assert client.get("/api/archive/tasks/missing/task-1/preview").status_code == 404
        assert client.delete(
            f"/api/islice-instances/{response.json()['id']}"
        ).status_code == 204


def test_archive_status_filters_channel_and_date_and_sorts_business_time_desc(
    tmp_path: Path, monkeypatch
) -> None:
    async def no_start(_self):
        return None

    async def no_stop(_self):
        return None

    channel_ids: dict[str, str] = {}

    async def fake_catalog(_self, _instances, _contexts):
        return {
            "sources": [],
            "tasks": [
                {
                    "task_id": "older-day",
                    "source_id": "islice-128",
                    "source_name": "iSlice 128",
                    "state": "deleted",
                    "archived_at": "2026-08-11T03:00:00+00:00",
                    "context": {
                        "channel_id": channel_ids["news"],
                        "channel_name": "新闻频道",
                        "broadcast_date": "2026-06-19",
                        "requested_start": 82800,
                    },
                },
                {
                    "task_id": "new-day-early-window",
                    "source_id": "islice-128",
                    "source_name": "iSlice 128",
                    "state": "deleted",
                    "archived_at": "2026-08-11T05:00:00+00:00",
                    "context": {
                        "channel_id": channel_ids["news"],
                        "channel_name": "新闻频道",
                        "broadcast_date": "2026-06-20",
                        "requested_start": 0,
                    },
                },
                {
                    "task_id": "new-day-late-window",
                    "source_id": "islice-128",
                    "source_name": "iSlice 128",
                    "state": "archived_hold",
                    "archived_at": "2026-08-11T04:00:00+00:00",
                    "context": {
                        "channel_id": channel_ids["news"],
                        "channel_name": "新闻频道",
                        "broadcast_date": "2026-06-20",
                        "requested_start": 3600,
                    },
                },
                {
                    "task_id": "other-channel",
                    "source_id": "islice-128",
                    "source_name": "iSlice 128",
                    "state": "deleted",
                    "archived_at": "2026-08-11T06:00:00+00:00",
                    "context": {
                        "channel_id": channel_ids["movie"],
                        "channel_name": "影视频道",
                        "broadcast_date": "2026-06-20",
                        "requested_start": 7200,
                    },
                },
            ],
        }

    monkeypatch.setattr(Orchestrator, "start", no_start)
    monkeypatch.setattr(Orchestrator, "stop", no_stop)
    monkeypatch.setattr(ArchiveCatalogReader, "read", fake_catalog)

    with TestClient(create_app(make_settings(tmp_path))) as client:
        channel_ids["news"] = client.post(
            "/api/channels", json={"name": "新闻频道"}
        ).json()["id"]
        channel_ids["movie"] = client.post(
            "/api/channels", json={"name": "影视频道"}
        ).json()["id"]

        payload = client.get("/api/archive/status?pageSize=20").json()
        assert [item["task_id"] for item in payload["items"]] == [
            "other-channel",
            "new-day-late-window",
            "new-day-early-window",
            "older-day",
        ]

        filtered = client.get(
            "/api/archive/status",
            params={
                "channelId": channel_ids["news"],
                "broadcastDate": "2026-06-20",
            },
        ).json()
        assert [item["task_id"] for item in filtered["items"]] == [
            "new-day-late-window",
            "new-day-early-window",
        ]
        invalid = client.get(
            "/api/archive/status", params={"broadcastDate": "2026-02-30"}
        )
        assert invalid.status_code == 422


def test_system_reset_requires_receipts_and_confirmation_then_preserves_media(
    tmp_path: Path, monkeypatch
) -> None:
    async def no_start(_self):
        return None

    async def no_stop(_self):
        return None

    monkeypatch.setattr(Orchestrator, "start", no_start)
    monkeypatch.setattr(Orchestrator, "stop", no_stop)
    settings = make_settings(tmp_path)
    media_marker = settings.data_dir / "jobs" / "completed-job" / "raw" / "response.json"
    temp_marker = settings.temp_dir / "completed-job" / "window-000.ts"

    with TestClient(create_app(settings)) as client:
        channel_id = client.post("/api/channels", json={"name": "待重置频道"}).json()["id"]

        async def seed_completed_job() -> None:
            database = Database(settings.database_path)
            await database.create_job(
                job_record(
                    tmp_path,
                    "completed-job",
                    status="completed",
                    channel_id=channel_id,
                    channel_name="待重置频道",
                )
            )
            window = await database.upsert_window("completed-job", 0, 0, 60)
            await database.create_attempt(window["id"], 1, "task-before-reset")

        asyncio.run(seed_completed_job())
        media_marker.parent.mkdir(parents=True)
        media_marker.write_text("keep", encoding="utf-8")
        temp_marker.parent.mkdir(parents=True)
        temp_marker.write_bytes(b"keep-media")

        preview_response = client.post("/api/system-reset/preview")
        assert preview_response.status_code == 200
        preview = preview_response.json()
        assert preview["counts"]["jobs"] == 1
        assert preview["counts"]["channels"] == 1
        assert preview["mediaDirectoriesIncluded"] is False
        assert len(preview["sources"]) == 1
        assert preview["sources"][0]["command"] == preview["sources"][0]["prepareCommand"]
        tokens = shlex.split(preview["sources"][0]["prepareCommand"])
        nonce = tokens[tokens.index("--nonce") + 1]
        source_id = preview["sources"][0]["sourceId"]
        receipt = {
            "requestId": preview["requestId"],
            "nonce": nonce,
            "sourceId": source_id,
            "preparedAt": "2026-08-11T12:00:00+00:00",
            "status": "prepared",
            "isliceDatabaseBackup": "/var/lib/islice-archiver/reset-backups/islice.db",
            "isliceDatabaseSha256": "a" * 64,
            "archiveDatabaseBackup": "/var/lib/islice-archiver/reset-backups/archive.db",
            "archiveDatabaseSha256": "b" * 64,
            "proof": "proof-token-value-with-enough-length",
            "mediaDirectoriesBackedUp": False,
        }
        wrong = client.post(
            "/api/system-reset/execute",
            json={
                "requestId": preview["requestId"],
                "confirmationText": "RESET wrong",
                "receipts": [receipt],
                "acknowledgeMediaHandling": True,
            },
        )
        assert wrong.status_code == 400
        assert client.get("/api/jobs").json()["total"] == 1

        receipt_with_wrong_nonce = {**receipt, "nonce": "x" * 24}
        rejected = client.post(
            "/api/system-reset/execute",
            json={
                "requestId": preview["requestId"],
                "confirmationText": preview["confirmationText"],
                "receipts": [receipt_with_wrong_nonce],
                "acknowledgeMediaHandling": True,
            },
        )
        assert rejected.status_code == 409
        assert client.get("/api/jobs").json()["total"] == 1

        executed = client.post(
            "/api/system-reset/execute",
            json={
                "requestId": preview["requestId"],
                "confirmationText": preview["confirmationText"],
                "receipts": [receipt],
                "acknowledgeMediaHandling": True,
            },
        )
        assert executed.status_code == 200
        result = executed.json()
        helper_backup = Path(result["helperBackup"]["databaseBackup"])
        assert helper_backup.is_file()
        with sqlite3.connect(helper_backup) as connection:
            assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
        assert result["mediaDirectoriesTouched"] is False
        assert len(result["commitCommands"]) == 1
        assert "--services-stopped" in result["commitCommands"][0]["command"]
        assert client.get("/api/jobs").json()["total"] == 0
        assert client.get("/api/channels").json() == []
        instances = client.get("/api/islice-instances").json()
        assert len(instances) == 1
        assert instances[0]["schedulable"] is False
        with sqlite3.connect(settings.database_path) as connection:
            for table in (
                "jobs", "channels", "windows", "attempts", "segments",
                "segment_merges", "segment_merge_members", "job_rebuilds",
            ):
                assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
            request_row = connection.execute(
                "SELECT status,helper_backup_path FROM system_reset_requests WHERE id=?",
                (preview["requestId"],),
            ).fetchone()
        assert request_row == ("helper_reset", str(helper_backup))
        assert media_marker.read_text(encoding="utf-8") == "keep"
        assert temp_marker.read_bytes() == b"keep-media"


def test_system_reset_preview_rejects_active_jobs(tmp_path: Path, monkeypatch) -> None:
    async def no_start(_self):
        return None

    async def no_stop(_self):
        return None

    monkeypatch.setattr(Orchestrator, "start", no_start)
    monkeypatch.setattr(Orchestrator, "stop", no_stop)
    settings = make_settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        async def seed_active_job() -> None:
            await Database(settings.database_path).create_job(
                job_record(tmp_path, "active-job", status="pending_schedule")
            )

        asyncio.run(seed_active_job())
        response = client.post("/api/system-reset/preview")
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "active_jobs"
