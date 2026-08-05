from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient

from slice_helper.app import create_app
from slice_helper.config import Settings
from slice_helper.database import Database
from slice_helper.media import MediaError
from slice_helper.models import MediaProbe
from slice_helper.orchestrator import Orchestrator
from slice_helper.source_download import SourceDownloadError
from slice_helper.time_ocr import OcrText, TimeReference


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

    async def fake_time_reference(_self, _source, frame_path, **_kwargs):
        frame_path.parent.mkdir(parents=True, exist_ok=True)
        frame_path.write_bytes(b"png")
        timestamp = datetime.fromisoformat("2026-06-20T12:29:59")
        return TimeReference(
            source_start_time=timestamp,
            observed_time=timestamp,
            frame_offset_seconds=0.0,
            matched_text="2026-06-20 12:29:59",
            confidence=0.99,
            ocr_texts=(OcrText("2026-06-20 12:29:59", 0.99),),
        )

    async def no_start(_self):
        return None

    async def no_stop(_self):
        return None

    monkeypatch.setattr("slice_helper.media.MediaService.probe", fake_probe)
    monkeypatch.setattr(
        "slice_helper.media.MediaService.detect_time_reference", fake_time_reference
    )
    monkeypatch.setattr(Orchestrator, "start", no_start)
    monkeypatch.setattr(Orchestrator, "stop", no_stop)

    source = tmp_path / "source.ts"
    source.write_bytes(b"source")
    configured = make_settings(tmp_path)

    with TestClient(create_app(configured)) as client:
        home = client.get("/")
        assert home.status_code == 200
        assert 'id="segmentPreview"' in home.text
        assert 'id="segmentPageInfo"' in home.text
        assert 'id="previousSegmentPage"' in home.text
        assert 'id="nextSegmentPage"' in home.text
        assert 'id="previewDialog"' not in home.text
        assert 'class="media-workspace"' in home.text
        assert 'class="segment-results-panel"' in home.text
        assert 'class="window-timeline-panel"' in home.text
        assert "小任务进度" in home.text
        assert 'id="summaryISlice"' in home.text
        assert "/static/styles.css?v=0.4.0" in home.text
        assert "/static/app.js?v=0.4.0" in home.text
        assert "TS 路径或 HTTP 地址" in home.text

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
        assert job["status"] == "pending_schedule"
        assert job["islice_base_url"] == ""
        assert job["program_start_time"] == "2026-06-20T12:29:59"
        assert job["time_reference_source"] == "ocr"
        assert job["time_reference_text"] == "2026-06-20 12:29:59"
        assert job["time_reference_confidence"] == 0.99
        assert Path(job["time_reference_frame_path"]).is_file()

        listed = client.get("/api/jobs").json()
        assert [item["id"] for item in listed] == [job_id]

        paused = client.post(f"/api/jobs/{job_id}/pause")
        assert paused.status_code == 200
        assert paused.json()["status"] == "paused"
        resumed = client.post(f"/api/jobs/{job_id}/resume")
        assert resumed.status_code == 200
        assert resumed.json()["status"] == "pending_schedule"

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


def test_create_job_downloads_http_source_to_managed_path(
    tmp_path: Path, monkeypatch
) -> None:
    downloaded: dict[str, object] = {}

    async def fake_download(_self, url, target):
        downloaded["url"] = url
        downloaded["target"] = target
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"downloaded-ts")
        return len(b"downloaded-ts")

    async def fake_probe(_self, path):
        assert path == downloaded["target"]
        return MediaProbe(
            duration=7200.0,
            format_name="mpegts",
            video_codec="h264",
            audio_codec="aac",
        )

    async def failed_time_reference(_self, _source, _frame_path, **_kwargs):
        raise MediaError("timestamp not found")

    async def no_start(_self):
        return None

    async def no_stop(_self):
        return None

    monkeypatch.setattr(
        "slice_helper.source_download.HttpSourceDownloader.download", fake_download
    )
    monkeypatch.setattr("slice_helper.media.MediaService.probe", fake_probe)
    monkeypatch.setattr(
        "slice_helper.media.MediaService.detect_time_reference", failed_time_reference
    )
    monkeypatch.setattr(Orchestrator, "start", no_start)
    monkeypatch.setattr(Orchestrator, "stop", no_stop)

    configured = make_settings(tmp_path)
    source_url = "https://media.test/archive/day.ts?token=internal"
    with TestClient(create_app(configured)) as client:
        response = client.post("/api/jobs", json={"sourcePath": source_url})

    assert response.status_code == 201
    job = response.json()
    managed_source = configured.data_dir / "jobs" / job["id"] / "source.ts"
    assert downloaded == {"url": source_url, "target": managed_source}
    assert managed_source.read_bytes() == b"downloaded-ts"
    assert job["source_url"] == source_url
    assert job["source_path"] == str(managed_source)
    assert job["source_size"] == len(b"downloaded-ts")
    assert job["total_windows"] == 2


def test_create_job_cleans_failed_http_download(tmp_path: Path, monkeypatch) -> None:
    async def failed_download(_self, _url, target):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.with_name("source.partial.ts").write_bytes(b"partial")
        raise SourceDownloadError("remote connection closed")

    async def no_start(_self):
        return None

    async def no_stop(_self):
        return None

    monkeypatch.setattr(
        "slice_helper.source_download.HttpSourceDownloader.download", failed_download
    )
    monkeypatch.setattr(Orchestrator, "start", no_start)
    monkeypatch.setattr(Orchestrator, "stop", no_stop)
    configured = make_settings(tmp_path)

    with TestClient(create_app(configured)) as client:
        response = client.post(
            "/api/jobs", json={"sourcePath": "http://media.test/broken.ts"}
        )

    assert response.status_code == 502
    assert response.json()["detail"] == "remote connection closed"
    jobs_dir = configured.data_dir / "jobs"
    assert not jobs_dir.exists() or not list(jobs_dir.iterdir())


def test_create_job_uses_manual_time_only_when_ocr_fails(
    tmp_path: Path, monkeypatch
) -> None:
    async def fake_probe(_self, _path):
        return MediaProbe(
            duration=120.0,
            format_name="mpegts",
            video_codec="h264",
            audio_codec="aac",
        )

    async def failed_time_reference(_self, _source, _frame_path, **_kwargs):
        raise MediaError("timestamp not found")

    async def no_start(_self):
        return None

    async def no_stop(_self):
        return None

    monkeypatch.setattr("slice_helper.media.MediaService.probe", fake_probe)
    monkeypatch.setattr(
        "slice_helper.media.MediaService.detect_time_reference", failed_time_reference
    )
    monkeypatch.setattr(Orchestrator, "start", no_start)
    monkeypatch.setattr(Orchestrator, "stop", no_stop)

    source = tmp_path / "source.ts"
    source.write_bytes(b"source")
    with TestClient(create_app(make_settings(tmp_path))) as client:
        response = client.post(
            "/api/jobs",
            json={
                "sourcePath": str(source.resolve()),
                "programStartTime": "2026-06-20T12:30:00+08:00",
            },
        )

    assert response.status_code == 201
    job = response.json()
    assert job["program_start_time"] == "2026-06-20T12:30:00+08:00"
    assert job["time_reference_source"] == "manual_fallback"
    assert job["time_reference_error"] == "timestamp not found"
    assert "fallback" in job["warnings"][0].lower()
