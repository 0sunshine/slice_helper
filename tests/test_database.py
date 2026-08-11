from __future__ import annotations

import json
from pathlib import Path

import pytest

from slice_helper.database import Database


@pytest.mark.asyncio
async def test_database_persists_and_recovers_jobs(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.db")
    await database.initialize()
    await database.create_job(
        {
            "id": "job1",
            "source_path": str(tmp_path / "source.ts"),
            "source_size": 10,
            "source_mtime_ns": 20,
            "source_duration": 7200.0,
            "template_id": "general",
            "language": "zh",
            "channel_name": "",
            "program_start_time": None,
            "cut_mode": "copy",
            "total_windows": 2,
        }
    )
    await database.update_job("job1", status="running")
    await database.recover_jobs()
    recovered = await database.get_job("job1")
    assert recovered["status"] == "pending_schedule"
    assert recovered["time_reference_source"] == ""
    assert recovered["time_reference_confidence"] is None

    async with database.connect() as db:
        versions = await (await db.execute("SELECT version FROM schema_version ORDER BY version")).fetchall()
    assert [row["version"] for row in versions] == list(range(1, 17))
    async with database.connect() as db:
        segment_columns = {
            row["name"]
            for row in await (await db.execute("PRAGMA table_info(segments)")).fetchall()
        }
    assert "content_type" in segment_columns
    assert "news_event_type" in segment_columns
    assert "attempt_id" in segment_columns
    assert "task_id" in segment_columns
    assert "ignored" in segment_columns
    async with database.connect() as db:
        attempt_columns = {
            row["name"]
            for row in await (await db.execute("PRAGMA table_info(attempts)")).fetchall()
        }
    assert "submitted_at" in attempt_columns
    async with database.connect() as db:
        job_columns = {
            row["name"]
            for row in await (await db.execute("PRAGMA table_info(jobs)")).fetchall()
        }
    assert "time_reference_frame_offset" in job_columns
    assert "reviewed" in job_columns
    assert "rebuild_revision" in job_columns
    assert recovered["reviewed"] == 0
    assert recovered["islice_base_url"] == ""
    assert recovered["source_url"] == ""

    window = await database.upsert_window("job1", 0, 0, 3600)
    same_window = await database.upsert_window("job1", 0, 99, 999)
    assert window["id"] == same_window["id"]
    assert same_window["requested_start"] == 0
    attempt = await database.create_attempt(window["id"], 1, "legacy-task")
    assert attempt["service_status"] == ""
    assert attempt["progress"] == 0
    assert attempt["submitted_at"] == ""
    await database.assign_legacy_jobs_with_attempts("http://islice-a.test/")
    assert (await database.get_job("job1"))["islice_base_url"] == "http://islice-a.test"


@pytest.mark.asyncio
async def test_tail_rebuild_truncates_atomically_and_cascades_results(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.db")
    await database.initialize()
    await database.create_job(
        {
            "id": "tail-job",
            "source_path": str(tmp_path / "source.ts"),
            "source_size": 10,
            "source_mtime_ns": 20,
            "source_duration": 10800.0,
            "islice_base_url": "http://islice.test",
            "template_id": "general",
            "language": "zh",
            "channel_name": "",
            "program_start_time": "2026-08-11T08:00:00",
            "cut_mode": "copy",
            "total_windows": 3,
            "status": "completed",
        }
    )
    await database.update_job(
        "tail-job", current_window=3, next_window_start=10800, progress=100, reviewed=1
    )
    for index in range(3):
        window = await database.upsert_window(
            "tail-job", index, index * 3600, (index + 1) * 3600
        )
        await database.update_window(
            window["id"],
            status="completed",
            handoff_start=(index + 1) * 3600,
            chunk_path=str(tmp_path / f"window-{index}.ts"),
        )
        attempt = await database.create_attempt(window["id"], 1, f"old-{index}")
        await database.update_attempt(
            attempt["id"], raw_response_path=str(tmp_path / f"raw-{index}.json")
        )
        await database.replace_window_segments(
            "tail-job",
            window["id"],
            [
                {
                    "source_index": 0,
                    "accepted": 1,
                    "reason": "",
                    "local_start": 0.0,
                    "local_end": 10.0,
                    "global_start": index * 3600.0,
                    "global_end": index * 3600.0 + 10,
                    "title": f"segment-{index}",
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

    preview = await database.get_rebuild_preview("tail-job", 1)
    assert preview is not None
    assert len(preview["windows"]) == 2
    assert len(preview["attempts"]) == 2
    assert len(preview["segments"]) == 2
    rebuild = await database.truncate_job_for_rebuild(
        "tail-job", 1, preview["preview_token"], str(tmp_path / "snapshot.json")
    )

    assert rebuild["generation"] == 1
    assert [item["window_index"] for item in await database.get_windows("tail-job")] == [0]
    assert [item["task_id"] for item in await database.get_attempts_for_job("tail-job")] == ["old-0"]
    assert [item["title"] for item in await database.get_segments("tail-job")] == ["segment-0"]
    job = await database.get_job("tail-job")
    assert job["status"] == "paused"
    assert job["current_window"] == 1
    assert job["next_window_start"] == 3600
    assert job["reviewed"] == 0
    assert job["rebuild_revision"] == 1
    assert set(json.loads(rebuild["task_ids_json"])) == {"old-1", "old-2"}


@pytest.mark.asyncio
async def test_tail_rebuild_from_first_window_resets_source_start(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.db")
    await database.initialize()
    await database.create_job(
        {
            "id": "first-job",
            "source_path": str(tmp_path / "source.ts"),
            "source_size": 10,
            "source_mtime_ns": 20,
            "source_duration": 3600.0,
            "islice_base_url": "http://islice.test",
            "template_id": "general",
            "language": "zh",
            "channel_name": "",
            "program_start_time": None,
            "cut_mode": "copy",
            "total_windows": 1,
            "status": "stopped",
        }
    )
    window = await database.upsert_window("first-job", 0, 27.0, 3600.0)
    await database.update_window(window["id"], status="completed")
    preview = await database.get_rebuild_preview("first-job", 0)
    await database.truncate_job_for_rebuild(
        "first-job", 0, preview["preview_token"], str(tmp_path / "snapshot.json")
    )
    job = await database.get_job("first-job")
    assert job["current_window"] == 0
    assert job["next_window_start"] == 0
    assert job["progress"] == 0


@pytest.mark.asyncio
async def test_database_assigns_multiple_jobs_to_the_same_islice(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.db")
    await database.initialize()
    urls = ("http://islice-a.test",)

    for job_id in ("job-a", "job-b", "job-c"):
        await database.create_job(
            {
                "id": job_id,
                "source_path": str(tmp_path / f"{job_id}.ts"),
                "source_size": 10,
                "source_mtime_ns": 20,
                "source_duration": 60.0,
                "template_id": "general",
                "language": "zh",
                "channel_name": "",
                "program_start_time": None,
                "cut_mode": "copy",
                "total_windows": 1,
            }
        )

    claimed = await database.claim_schedulable_jobs(urls, 3)
    assert [(job["id"], job["islice_base_url"]) for job in claimed] == [
        ("job-a", urls[0]),
        ("job-b", urls[0]),
        ("job-c", urls[0]),
    ]
    for job_id in ("job-a", "job-b", "job-c"):
        assert (await database.get_job(job_id))["status"] == "queued"


@pytest.mark.asyncio
async def test_paused_job_keeps_assignment_without_reserving_islice(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.db")
    await database.initialize()
    common = {
        "source_path": str(tmp_path / "source.ts"),
        "source_size": 10,
        "source_mtime_ns": 20,
        "source_duration": 60.0,
        "template_id": "general",
        "language": "zh",
        "channel_name": "",
        "program_start_time": None,
        "cut_mode": "copy",
        "total_windows": 1,
    }
    await database.create_job(
        {
            **common,
            "id": "bound-job",
            "status": "paused",
            "islice_base_url": "http://islice-a.test",
        }
    )
    await database.create_job({**common, "id": "new-job"})

    claimed = await database.claim_schedulable_jobs(("http://islice-a.test",), 1)
    assert [(job["id"], job["islice_base_url"]) for job in claimed] == [
        ("new-job", "http://islice-a.test")
    ]
    assert (await database.get_job("bound-job"))["islice_base_url"] == (
        "http://islice-a.test"
    )
