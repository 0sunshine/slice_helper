from __future__ import annotations

import io
from pathlib import Path

import openpyxl
import pytest

from slice_helper.database import Database
from slice_helper.excel_export import build_channel_workbook


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
    assert [row["version"] for row in versions] == list(range(1, 27))
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
    assert "review_status" in attempt_columns
    assert "reviewed_at" in attempt_columns
    assert "ai_review_score" in attempt_columns
    assert "ai_review_comment" in attempt_columns
    async with database.connect() as db:
        attempt_indexes = {
            row["name"]
            for row in await (await db.execute("PRAGMA index_list(attempts)")).fetchall()
        }
        segment_indexes = {
            row["name"]
            for row in await (await db.execute("PRAGMA index_list(segments)")).fetchall()
        }
    assert "idx_attempts_status_submitted" in attempt_indexes
    assert "idx_segments_attempt" in segment_indexes
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
    assert attempt["review_status"] == "unreviewed"
    assert attempt["ai_review_score"] is None
    assert attempt["ai_review_comment"] == ""
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
    assert job["status"] == "pending_schedule"
    assert job["current_window"] == 1
    assert job["next_window_start"] == 3600
    assert job["reviewed"] == 0
    assert job["rebuild_revision"] == 1
    assert rebuild["status"] == "queued"


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
async def test_database_schedules_job_with_fewest_completed_tasks_first(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "state.db")
    await database.initialize()
    common = {
        "source_path": str(tmp_path / "source.ts"),
        "source_size": 10,
        "source_mtime_ns": 20,
        "source_duration": 14400.0,
        "template_id": "general",
        "language": "zh",
        "channel_name": "",
        "program_start_time": None,
        "cut_mode": "copy",
        "total_windows": 4,
    }
    await database.create_job({**common, "id": "older-ahead"})
    await database.update_job("older-ahead", current_window=2, progress=50)
    await database.create_job({**common, "id": "newer-behind"})
    await database.update_job("newer-behind", current_window=1, progress=25)

    claimed = await database.claim_schedulable_jobs(("http://islice.test",), 1)

    assert [job["id"] for job in claimed] == ["newer-behind"]


@pytest.mark.asyncio
async def test_database_caps_active_jobs_at_ten_per_islice(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.db")
    await database.initialize()
    urls = tuple(f"http://islice-{name}.test" for name in "abcd")
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
    for index in range(43):
        await database.create_job({**common, "id": f"job-{index:02d}"})

    claimed = await database.claim_schedulable_jobs(urls, 43)

    assert len(claimed) == 40
    assert {
        url: sum(job["islice_base_url"] == url for job in claimed) for url in urls
    } == {url: 10 for url in urls}
    pending = [
        await database.get_job(f"job-{index:02d}") for index in range(40, 43)
    ]
    assert all(job and job["status"] == "pending_schedule" for job in pending)


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


@pytest.mark.asyncio
async def test_paused_submitted_attempt_reserves_islice(tmp_path: Path) -> None:
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
    await database.create_job({**common, "id": "paused-job", "status": "paused", "islice_base_url": "http://islice-a.test"})
    window = await database.upsert_window("paused-job", 0, 0, 60)
    attempt = await database.create_attempt(window["id"], 1, "task-paused")
    await database.update_attempt(
        attempt["id"], status="polling", service_status="processing",
        progress=20, submitted_at="2026-08-13T00:00:00+00:00",
    )
    await database.create_job({**common, "id": "new-job"})
    claimed = await database.claim_schedulable_jobs(
        ("http://islice-a.test", "http://islice-b.test"), 1
    )
    assert [job["id"] for job in claimed] == ["new-job"]
    assert claimed[0]["islice_base_url"] == "http://islice-b.test"


def merge_segment(
    source_index: int,
    start: float,
    end: float,
    title: str,
    *,
    accepted: int = 1,
    ignored: int = 0,
) -> dict:
    return {
        "source_index": source_index,
        "accepted": accepted,
        "ignored": ignored,
        "reason": "" if accepted else "discarded",
        "local_start": start,
        "local_end": end,
        "global_start": start,
        "global_end": end,
        "title": title,
        "content_type": "新闻",
        "news_event_type": f"事件-{title}",
        "topic": f"主题-{title}",
        "keywords_json": f'["关键词-{title}"]',
        "summary": f"摘要-{title}",
        "segment_url": "",
        "cover_img_url": "",
        "raw_json": "{}",
    }


async def prepare_merge_job(database: Database, tmp_path: Path) -> tuple[str, str, int]:
    channel = await database.create_channel("合并测试频道")
    job_id = "merge-job"
    await database.create_job(
        {
            "id": job_id,
            "source_path": str(tmp_path / "source.ts"),
            "source_size": 10,
            "source_mtime_ns": 20,
            "source_duration": 120.0,
            "template_id": "general",
            "language": "zh",
            "channel_id": channel["id"],
            "channel_name": channel["name"],
            "broadcast_date": "2026-08-11",
            "program_start_time": "2026-08-11T08:00:00+08:00",
            "cut_mode": "copy",
            "total_windows": 1,
            "status": "completed",
        }
    )
    window = await database.upsert_window(job_id, 0, 0, 120)
    await database.update_window(window["id"], status="completed", handoff_start=120)
    attempt = await database.create_attempt(window["id"], 1, "merge-task")
    await database.update_attempt(attempt["id"], status="completed")
    await database.replace_window_segments(
        job_id,
        window["id"],
        [
            merge_segment(0, 0, 10, "第一条"),
            merge_segment(1, 10, 20, "主条目"),
            merge_segment(2, 20, 30, "第三条"),
            merge_segment(3, 30, 40, "舍弃条目", accepted=0),
        ],
    )
    return job_id, str(channel["id"]), int(window["id"])


@pytest.mark.asyncio
async def test_manual_segment_merge_lifecycle_and_effective_export(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.db")
    await database.initialize()
    job_id, channel_id, window_id = await prepare_merge_job(database, tmp_path)
    raw = await database.get_segments(job_id)
    first, primary, third, discarded = [
        next(item for item in raw if item["title"] == title)
        for title in ("第一条", "主条目", "第三条", "舍弃条目")
    ]

    with pytest.raises(ValueError, match="consecutive"):
        await database.preview_segment_merge(
            job_id, [first["id"], third["id"]], first["id"]
        )
    with pytest.raises(ValueError, match="final adopted"):
        await database.preview_segment_merge(
            job_id, [third["id"], discarded["id"]], third["id"]
        )

    stale = await database.preview_segment_merge(
        job_id, [first["id"], primary["id"]], primary["id"]
    )
    await database.update_segment(job_id, primary["id"], title="更新后的主条目")
    with pytest.raises(ValueError, match="changed after preview"):
        await database.create_segment_merge(
            job_id,
            [first["id"], primary["id"]],
            primary["id"],
            stale["preview_token"],
        )

    preview = await database.preview_segment_merge(
        job_id, [first["id"], primary["id"]], primary["id"]
    )
    assert preview["global_start"] == 0
    assert preview["global_end"] == 20
    assert preview["gap_seconds"] == 0
    assert preview["primary"]["title"] == "更新后的主条目"
    merge = await database.create_segment_merge(
        job_id,
        [first["id"], primary["id"]],
        primary["id"],
        preview["preview_token"],
    )
    assert (await database.get_job(job_id))["reviewed"] == 0

    displayed = await database.get_segments(job_id)
    merged = next(item for item in displayed if item["record_kind"] == "merge")
    assert merged["title"] == "更新后的主条目"
    assert merged["news_event_type"] == "事件-主条目"
    assert merged["topic"] == "主题-主条目"
    assert merged["keywords_json"] == '["关键词-主条目"]'
    assert merged["summary"] == "摘要-主条目"
    assert merged["member_count"] == 2
    assert len([item for item in displayed if item.get("active_merge_id")]) == 2
    with pytest.raises(ValueError, match="Cancel the manual merge"):
        await database.update_segment(job_id, first["id"], title="不允许")
    with pytest.raises(ValueError, match="already belongs"):
        await database.preview_segment_merge(
            job_id, [first["id"], primary["id"]], first["id"]
        )

    exported = await database.get_channel_export(channel_id)
    assert [item["title"] for item in exported["segments"]] == [
        "更新后的主条目",
        "第三条",
    ]
    assert exported["segments"][0]["manual_merge"] == 1
    workbook = openpyxl.load_workbook(io.BytesIO(build_channel_workbook(exported)))
    sheet = workbook["2026-08-11"]
    assert sheet.max_row == 3
    assert sheet["B2"].value == "更新后的主条目"
    assert sheet["AM2"].value == "是"
    assert sheet["V2"].value is None
    assert {sheet["B2"].value, sheet["B3"].value} == {
        "更新后的主条目",
        "第三条",
    }

    await database.update_job(job_id, reviewed=1)
    updated = await database.update_segment_merge(
        job_id, merge["id"], title="手工合并标题", ignored=1
    )
    assert updated["title"] == "手工合并标题"
    assert (await database.get_job(job_id))["reviewed"] == 0
    ignored_export = await database.get_channel_export(channel_id)
    assert [item["title"] for item in ignored_export["segments"]] == ["第三条"]

    await database.update_segment_merge(job_id, merge["id"], ignored=0)
    await database.update_job(job_id, reviewed=1)
    assert await database.cancel_segment_merge(job_id, merge["id"])
    assert (await database.get_job(job_id))["reviewed"] == 0
    restored_export = await database.get_channel_export(channel_id)
    assert [item["title"] for item in restored_export["segments"]] == [
        "第一条",
        "更新后的主条目",
        "第三条",
    ]

    second_preview = await database.preview_segment_merge(
        job_id, [first["id"], primary["id"]], first["id"]
    )
    second_merge = await database.create_segment_merge(
        job_id,
        [first["id"], primary["id"]],
        first["id"],
        second_preview["preview_token"],
    )
    await database.update_job(job_id, reviewed=1)
    await database.replace_window_segments(
        job_id, window_id, [merge_segment(0, 0, 15, "重拆结果")]
    )
    history = await database.get_job_merges(job_id)
    invalidated = next(item for item in history if item["id"] == second_merge["id"])
    assert invalidated["status"] == "invalidated"
    assert invalidated["cancellation_reason"] == "window resplit replaced a member"
    assert all(member["active"] == 0 for member in invalidated["members"])
    assert all(member["snapshot_json"] for member in invalidated["members"])
    assert (await database.get_job(job_id))["reviewed"] == 0


@pytest.mark.asyncio
async def test_tail_rebuild_invalidates_cross_window_manual_merge(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.db")
    await database.initialize()
    await database.create_job(
        {
            "id": "cross-window-job",
            "source_path": str(tmp_path / "source.ts"),
            "source_size": 10,
            "source_mtime_ns": 20,
            "source_duration": 120.0,
            "islice_base_url": "http://islice.test",
            "template_id": "general",
            "language": "zh",
            "channel_name": "",
            "program_start_time": "2026-08-11T08:00:00+08:00",
            "cut_mode": "copy",
            "total_windows": 2,
            "status": "completed",
        }
    )
    for index in range(2):
        window = await database.upsert_window(
            "cross-window-job", index, index * 60, (index + 1) * 60
        )
        await database.update_window(
            window["id"], status="completed", handoff_start=(index + 1) * 60
        )
        await database.replace_window_segments(
            "cross-window-job",
            window["id"],
            [merge_segment(index, index * 60, index * 60 + 10, f"窗口{index + 1}")],
        )
    segments = await database.get_segments("cross-window-job")
    preview = await database.preview_segment_merge(
        "cross-window-job",
        [segments[0]["id"], segments[1]["id"]],
        segments[0]["id"],
    )
    merge = await database.create_segment_merge(
        "cross-window-job",
        [segments[0]["id"], segments[1]["id"]],
        segments[0]["id"],
        preview["preview_token"],
    )
    rebuild_preview = await database.get_rebuild_preview("cross-window-job", 1)
    assert len(rebuild_preview["merges"]) == 1
    await database.truncate_job_for_rebuild(
        "cross-window-job",
        1,
        rebuild_preview["preview_token"],
        str(tmp_path / "snapshot.json"),
    )
    history = await database.get_job_merges("cross-window-job")
    invalidated = next(item for item in history if item["id"] == merge["id"])
    assert invalidated["status"] == "invalidated"
    assert invalidated["cancellation_reason"] == "tail rebuild removed a member"
    assert not any(item["record_kind"] == "merge" for item in await database.get_segments("cross-window-job"))
