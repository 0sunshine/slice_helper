from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi.testclient import TestClient

from slice_helper.app import create_app
from slice_helper.config import Settings
from slice_helper.database import Database
from slice_helper.models import MediaProbe
from slice_helper.orchestrator import Orchestrator


def make_settings(tmp_path: Path) -> Settings:
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
        poll_interval_seconds=0.01,
        window_timeout_seconds=1,
        ffmpeg_timeout_seconds=10,
    )


def test_job_api_control_and_database_backed_chunk_route(
    tmp_path: Path, monkeypatch
) -> None:
    async def fake_probe(_self, _path):
        return MediaProbe(
            duration=120.0,
            format_name="mpegts",
            video_codec="h264",
            audio_codec="aac",
        )

    async def no_start(_self):
        return None

    async def no_stop(_self):
        return None

    monkeypatch.setattr("slice_helper.media.MediaService.probe", fake_probe)
    monkeypatch.setattr(Orchestrator, "start", no_start)
    monkeypatch.setattr(Orchestrator, "stop", no_stop)

    source = tmp_path / "source.ts"
    source.write_bytes(b"source")
    configured = make_settings(tmp_path)

    with TestClient(create_app(configured)) as client:
        created = client.post(
            "/api/jobs",
            json={
                "sourcePath": str(source.resolve()),
                "templateId": "general",
                "language": "zh",
                "cutMode": "copy",
            },
        )
        assert created.status_code == 201
        job = created.json()
        job_id = job["id"]
        assert job["total_windows"] == 1

        listed = client.get("/api/jobs").json()
        assert [item["id"] for item in listed] == [job_id]

        paused = client.post(f"/api/jobs/{job_id}/pause")
        assert paused.status_code == 200
        assert paused.json()["status"] == "paused"
        resumed = client.post(f"/api/jobs/{job_id}/resume")
        assert resumed.status_code == 200
        assert resumed.json()["status"] == "queued"

        chunk = configured.temp_dir / job_id / "window-000.ts"
        chunk.parent.mkdir(parents=True)
        chunk.write_bytes(b"test-ts-body")

        async def record_chunk() -> None:
            database = Database(configured.database_path)
            window = await database.upsert_window(job_id, 0, 0, 120)
            await database.update_window(
                window["id"], chunk_path=str(chunk), chunk_url="http://helper/chunk"
            )

        asyncio.run(record_chunk())
        response = client.get(f"/internal/chunks/{job_id}/0.ts")
        assert response.status_code == 200
        assert response.content == b"test-ts-body"
        assert response.headers["content-type"].startswith("video/mp2t")
        assert response.headers["content-length"] == str(len(b"test-ts-body"))
        assert client.get("/internal/chunks/not-a-job/0.ts").status_code == 404

        stopped = client.post(f"/api/jobs/{job_id}/stop")
        assert stopped.status_code == 200
        assert stopped.json()["status"] == "stopped"


def test_create_job_rejects_non_absolute_ts_path(tmp_path: Path) -> None:
    configured = make_settings(tmp_path)
    with TestClient(create_app(configured)) as client:
        response = client.post(
            "/api/jobs",
            json={"sourcePath": "relative.ts", "templateId": "general", "language": "zh"},
        )
    assert response.status_code == 422
