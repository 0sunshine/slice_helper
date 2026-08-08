from __future__ import annotations

import asyncio
import io
import zipfile
from datetime import datetime, timedelta
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


def create_channel(client: TestClient, name: str = "测试频道") -> str:
    response = client.post("/api/channels", json={"name": name})
    assert response.status_code == 201
    return response.json()["id"]


def test_job_api_control_and_database_backed_chunk_route(
    tmp_path: Path, monkeypatch
) -> None:
    resplit_call: dict[str, object] = {}
    overlap_call: dict[str, object] = {}

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

    async def fake_resplit(_self, job_id, window_index, task_id):
        resplit_call.update(
            job_id=job_id, window_index=window_index, task_id=task_id
        )
        return {
            "jobId": job_id,
            "windowIndex": window_index,
            "taskId": task_id,
            "status": "resplit_queued",
        }

    async def fake_accept_overlap(_self, job_id, window_index, task_id):
        overlap_call.update(
            job_id=job_id, window_index=window_index, task_id=task_id
        )
        return {
            "jobId": job_id,
            "windowIndex": window_index,
            "taskId": task_id,
            "status": "overlap_accepted",
        }

    monkeypatch.setattr("slice_helper.media.MediaService.probe", fake_probe)
    monkeypatch.setattr(
        "slice_helper.media.MediaService.detect_time_reference", fake_time_reference
    )
    monkeypatch.setattr(Orchestrator, "start", no_start)
    monkeypatch.setattr(Orchestrator, "stop", no_stop)
    monkeypatch.setattr(Orchestrator, "schedule_resplit", fake_resplit)
    monkeypatch.setattr(
        Orchestrator, "accept_resplit_overlap", fake_accept_overlap
    )

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
        assert 'id="resplitDialog"' in home.text
        assert 'id="resplitForm"' in home.text
        assert "确认重新拆分" in home.text
        assert "<th>操作</th>" in home.text
        assert "小任务进度" in home.text
        assert "<th>节目类型</th>" in home.text
        assert "<th>新闻事件</th>" in home.text
        assert 'id="summaryISlice"' in home.text
        assert "/static/styles.css?v=0.7.0" in home.text
        assert "/static/app.js?v=0.7.0" in home.text
        assert "TS 路径或 HTTP 地址" in home.text
        assert 'id="manageChannelsButton"' in home.text
        assert 'id="jobPageInfo"' in home.text

        channel_id = create_channel(client)

        created = client.post(
            "/api/jobs",
            json={
                "sourcePath": str(source.resolve()),
                "channelId": channel_id,
                "broadcastDate": "2026-06-20",
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
        assert job["channel_name"] == "测试频道"
        assert job["broadcast_date"] == "2026-06-20"
        assert job["program_start_time"] == "2026-06-20T12:29:59"
        assert job["time_reference_source"] == "ocr"
        assert job["time_reference_text"] == "2026-06-20 12:29:59"
        assert job["time_reference_confidence"] == 0.99
        assert Path(job["time_reference_frame_path"]).is_file()

        listed = client.get("/api/jobs").json()
        assert listed["total"] == 1
        assert [item["id"] for item in listed["items"]] == [job_id]

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

        resplit = client.post(
            f"/api/jobs/{job_id}/windows/0/resplit",
            json={"taskId": "sh-test-w000-a1"},
        )
        assert resplit.status_code == 202
        assert resplit.json()["taskId"] == "sh-test-w000-a1"
        assert resplit_call == {
            "job_id": job_id,
            "window_index": 0,
            "task_id": "sh-test-w000-a1",
        }

        overlap = client.post(
            f"/api/jobs/{job_id}/windows/0/accept-overlap",
            json={"taskId": "sh-test-w000-a1"},
        )
        assert overlap.status_code == 200
        assert overlap.json()["status"] == "overlap_accepted"
        assert overlap_call == {
            "job_id": job_id,
            "window_index": 0,
            "task_id": "sh-test-w000-a1",
        }

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
        channel_id = create_channel(client)
        response = client.post(
            "/api/jobs",
            json={
                "sourcePath": source_url,
                "channelId": channel_id,
                "broadcastDate": "2026-06-20",
            },
        )

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
        channel_id = create_channel(client)
        response = client.post(
            "/api/jobs",
            json={
                "sourcePath": "http://media.test/broken.ts",
                "channelId": channel_id,
                "broadcastDate": "2026-06-20",
            },
        )

    assert response.status_code == 502
    assert response.json()["detail"] == "remote connection closed"
    jobs_dir = configured.data_dir / "jobs"
    assert not jobs_dir.exists() or not list(jobs_dir.iterdir())


def test_create_job_uses_manual_time_only_when_ocr_fails(
    tmp_path: Path, monkeypatch
) -> None:
    attempted_offsets: list[float] = []

    async def fake_probe(_self, _path):
        return MediaProbe(
            duration=120.0,
            format_name="mpegts",
            video_codec="h264",
            audio_codec="aac",
        )

    async def failed_time_reference(_self, _source, _frame_path, **kwargs):
        attempted_offsets.append(kwargs["frame_offset_seconds"])
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
        channel_id = create_channel(client)
        response = client.post(
            "/api/jobs",
            json={
                "sourcePath": str(source.resolve()),
                "channelId": channel_id,
                "broadcastDate": "2026-06-20",
                "programStartTime": "2026-06-20T12:30:00+08:00",
            },
        )

    assert response.status_code == 201
    job = response.json()
    assert job["program_start_time"] == "2026-06-20T12:30:00+08:00"
    assert job["time_reference_source"] == "manual_fallback"
    assert attempted_offsets == [0.0, 60.0, 120.0, 180.0, 240.0, 300.0]
    assert job["time_reference_error"].startswith("0s: timestamp not found")
    assert job["time_reference_error"].endswith("300s: timestamp not found")
    assert "fallback" in job["warnings"][0].lower()


def test_create_job_retries_ocr_and_back_calculates_first_frame_time(
    tmp_path: Path, monkeypatch
) -> None:
    attempted_offsets: list[float] = []

    async def fake_probe(_self, _path):
        return MediaProbe(
            duration=600.0,
            format_name="mpegts",
            video_codec="h264",
            audio_codec="aac",
        )

    async def retrying_time_reference(_self, _source, frame_path, **kwargs):
        offset = kwargs["frame_offset_seconds"]
        attempted_offsets.append(offset)
        if offset < 120:
            raise MediaError("timestamp not found")
        frame_path.parent.mkdir(parents=True, exist_ok=True)
        frame_path.write_bytes(b"png")
        observed = datetime.fromisoformat("2026-06-20T12:32:00")
        return TimeReference(
            source_start_time=observed - timedelta(seconds=offset),
            observed_time=observed,
            frame_offset_seconds=offset,
            matched_text="2026-06-20 12:32:00",
            confidence=0.98,
            ocr_texts=(OcrText("2026-06-20 12:32:00", 0.98),),
        )

    async def no_start(_self):
        return None

    async def no_stop(_self):
        return None

    monkeypatch.setattr("slice_helper.media.MediaService.probe", fake_probe)
    monkeypatch.setattr(
        "slice_helper.media.MediaService.detect_time_reference",
        retrying_time_reference,
    )
    monkeypatch.setattr(Orchestrator, "start", no_start)
    monkeypatch.setattr(Orchestrator, "stop", no_stop)

    source = tmp_path / "source.ts"
    source.write_bytes(b"source")
    with TestClient(create_app(make_settings(tmp_path))) as client:
        channel_id = create_channel(client)
        response = client.post(
            "/api/jobs",
            json={
                "sourcePath": str(source.resolve()),
                "channelId": channel_id,
                "broadcastDate": "2026-06-20",
            },
        )

    assert response.status_code == 201
    job = response.json()
    assert attempted_offsets == [0.0, 60.0, 120.0]
    assert job["program_start_time"] == "2026-06-20T12:30:00"
    assert job["time_reference_frame_offset"] == 120.0
    assert job["status"] == "pending_schedule"
    assert "120s" in job["warnings"][0]


def test_missing_time_stops_until_page_correction_and_resyncs_real_times(
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
    configured = make_settings(tmp_path)
    with TestClient(create_app(configured)) as client:
        channel_id = create_channel(client)
        created = client.post(
            "/api/jobs",
            json={
                "sourcePath": str(source.resolve()),
                "channelId": channel_id,
                "broadcastDate": "2026-06-20",
            },
        )
        assert created.status_code == 201
        job = created.json()
        job_id = job["id"]
        assert job["status"] == "stopped"
        assert job["program_start_time"] is None
        assert "job stopped" in job["error_message"]

        async def add_existing_result() -> None:
            database = Database(configured.database_path)
            window = await database.upsert_window(job_id, 0, 60.0, 120.0)
            attempt = await database.create_attempt(window["id"], 1, "task-time-fix")
            await database.update_attempt(attempt["id"], status="completed")
            await database.replace_window_segments(
                job_id,
                window["id"],
                [
                    {
                        "source_index": 0,
                        "accepted": 1,
                        "reason": "",
                        "local_start": 10.0,
                        "local_end": 20.0,
                        "global_start": 70.0,
                        "global_end": 80.0,
                        "absolute_start": None,
                        "absolute_end": None,
                        "title": "测试片段",
                        "content_type": "新闻",
                        "news_event_type": "",
                        "topic": "",
                        "keywords_json": "[]",
                        "summary": "",
                        "segment_url": "",
                        "cover_img_url": "",
                        "raw_json": "{}",
                    }
                ],
            )

        asyncio.run(add_existing_result())
        corrected = client.patch(
            f"/api/jobs/{job_id}/time-reference",
            json={"programStartTime": "2026-06-20T12:00:00+08:00"},
        )
        assert corrected.status_code == 200
        payload = corrected.json()
        assert payload["updatedSegmentCount"] == 1
        assert payload["job"]["status"] == "paused"
        assert payload["job"]["time_reference_source"] == "manual_override"

        detail = client.get(f"/api/jobs/{job_id}").json()
        assert detail["attempts"][0]["program_start_time"] == (
            "2026-06-20T12:01:00+08:00"
        )
        segment = client.get(f"/api/jobs/{job_id}/segments").json()[0]
        assert segment["absolute_start"] == "2026-06-20T12:01:10+08:00"
        assert segment["absolute_end"] == "2026-06-20T12:01:20+08:00"

        resumed = client.post(f"/api/jobs/{job_id}/resume")
        assert resumed.status_code == 200
        assert resumed.json()["status"] == "pending_schedule"


def test_channel_date_overwrite_pagination_and_excel_export(
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

    first_source = tmp_path / "first.ts"
    second_source = tmp_path / "second.ts"
    first_source.write_bytes(b"first")
    second_source.write_bytes(b"second")
    configured = make_settings(tmp_path)

    with TestClient(create_app(configured)) as client:
        channel_id = create_channel(client, "测试频道")
        duplicate_channel = client.post(
            "/api/channels", json={"name": "  测试频道  "}
        )
        assert duplicate_channel.status_code == 409
        empty_channel_id = create_channel(client, "待删除频道")
        assert client.delete(f"/api/channels/{empty_channel_id}").status_code == 204

        common = {
            "channelId": channel_id,
            "broadcastDate": "2026-06-20",
            "programStartTime": "2026-06-20T00:00:00+08:00",
        }
        first = client.post(
            "/api/jobs", json={**common, "sourcePath": str(first_source.resolve())}
        )
        assert first.status_code == 201
        first_id = first.json()["id"]

        conflict = client.post(
            "/api/jobs", json={**common, "sourcePath": str(second_source.resolve())}
        )
        assert conflict.status_code == 409
        assert conflict.json()["detail"]["code"] == "channel_date_exists"

        database = Database(configured.database_path)
        asyncio.run(database.update_job(first_id, status="completed"))
        replacement = client.post(
            "/api/jobs",
            json={
                **common,
                "sourcePath": str(second_source.resolve()),
                "overwrite": True,
            },
        )
        assert replacement.status_code == 201
        replacement_id = replacement.json()["id"]
        historical = asyncio.run(database.get_job(first_id))
        assert historical["superseded_by_job_id"] == replacement_id
        assert historical["superseded_at"]

        active_conflict = client.post(
            "/api/jobs",
            json={
                **common,
                "sourcePath": str(first_source.resolve()),
                "overwrite": True,
            },
        )
        assert active_conflict.status_code == 409
        assert active_conflict.json()["detail"]["code"] == "channel_date_active"

        async def add_results() -> None:
            await database.update_job(replacement_id, status="completed")
            window = await database.upsert_window(replacement_id, 0, 0, 120)
            attempt = await database.create_attempt(window["id"], 1, "task-export-1")
            await database.update_attempt(
                attempt["id"], status="completed", service_status="completed", progress=100
            )
            base = {
                "reason": "",
                "local_start": 0.0,
                "local_end": 30.0,
                "global_start": 0.0,
                "global_end": 30.0,
                "absolute_start": "2026-06-20T00:00:00+08:00",
                "absolute_end": "2026-06-20T00:00:30+08:00",
                "content_type": "新闻",
                "news_event_type": "时政要闻",
                "topic": "时政",
                "keywords_json": '["关键词一", "关键词二"]',
                "segment_url": "http://media.test/accepted.mp4",
                "cover_img_url": "http://media.test/accepted.jpg",
            }
            await database.replace_window_segments(
                replacement_id,
                window["id"],
                [
                    {
                        **base,
                        "source_index": 0,
                        "accepted": 1,
                        "title": "最终采用标题",
                        "summary": "界面未显示的完整摘要",
                        "raw_json": "{}",
                    },
                    {
                        **base,
                        "source_index": 1,
                        "accepted": 0,
                        "reason": "handoff",
                        "title": "不应导出的舍弃标题",
                        "summary": "舍弃摘要",
                        "raw_json": "{}",
                    },
                ],
            )

        asyncio.run(add_results())
        second_date = client.post(
            "/api/jobs",
            json={
                "sourcePath": str(first_source.resolve()),
                "channelId": channel_id,
                "broadcastDate": "2026-06-21",
            },
        )
        assert second_date.status_code == 201

        page_one = client.get("/api/jobs", params={"page": 1, "pageSize": 1}).json()
        page_two = client.get("/api/jobs", params={"page": 2, "pageSize": 1}).json()
        assert page_one["total"] == 2
        assert page_one["totalPages"] == 2
        assert page_one["items"][0]["id"] != page_two["items"][0]["id"]
        filtered = client.get(
            "/api/jobs",
            params={"channelId": channel_id, "broadcastDate": "2026-06-20"},
        ).json()
        assert filtered["total"] == 1
        assert filtered["items"][0]["id"] == replacement_id

        exported = client.get(f"/api/channels/{channel_id}/export.xlsx")
        assert exported.status_code == 200
        assert exported.headers["content-type"].startswith(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
            workbook_xml = archive.read("xl/workbook.xml").decode("utf-8")
            shared_strings = archive.read("xl/sharedStrings.xml").decode("utf-8")
            first_sheet = archive.read("xl/worksheets/sheet1.xml").decode("utf-8")
        assert "2026-06-20" in workbook_xml
        assert "2026-06-21" in workbook_xml
        assert "最终采用标题" in shared_strings
        assert "界面未显示的完整摘要" in shared_strings
        assert "task-export-1" in shared_strings
        assert "不应导出的舍弃标题" not in shared_strings
        assert "IF(AND(D2" in first_sheet

        renamed = client.patch(
            f"/api/channels/{channel_id}", json={"name": "测试频道（新）"}
        )
        assert renamed.status_code == 200
        assert renamed.json()["name"] == "测试频道（新）"
        assert client.delete(f"/api/channels/{channel_id}").status_code == 409
