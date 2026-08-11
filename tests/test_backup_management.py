from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from slice_helper.app import create_app
from slice_helper.archive_status import ArchiveCatalogReader
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
        max_active_jobs=2,
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
    ]
    try:
        result = await reader.read(
            instances,
            {("islice-good", "task-1"): {"job_id": "job-1"}},
        )
    finally:
        await reader.close()
    assert [source["online"] for source in result["sources"]] == [True, False, False]
    assert "不匹配" in result["sources"][1]["error"]
    assert "未配置" in result["sources"][2]["error"]
    assert result["tasks"][0]["context"]["job_id"] == "job-1"


def test_backup_page_and_instance_api(tmp_path: Path, monkeypatch) -> None:
    async def no_start(_self):
        return None

    async def no_stop(_self):
        return None

    async def fake_catalog(_self, instances, _contexts):
        return {"sources": [], "tasks": []}

    monkeypatch.setattr(Orchestrator, "start", no_start)
    monkeypatch.setattr(Orchestrator, "stop", no_stop)
    monkeypatch.setattr(ArchiveCatalogReader, "read", fake_catalog)

    with TestClient(create_app(make_settings(tmp_path))) as client:
        page = client.get("/backup")
        assert page.status_code == 200
        assert "iSlice 实例与备份" in page.text
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
        status = client.get("/api/archive/status").json()
        assert status["total"] == 0
        assert client.delete(
            f"/api/islice-instances/{response.json()['id']}"
        ).status_code == 204
