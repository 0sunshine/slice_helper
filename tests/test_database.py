from __future__ import annotations

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
    assert [row["version"] for row in versions] == [1, 2, 3, 4, 5]
    assert recovered["islice_base_url"] == ""

    window = await database.upsert_window("job1", 0, 0, 3600)
    same_window = await database.upsert_window("job1", 0, 99, 999)
    assert window["id"] == same_window["id"]
    assert same_window["requested_start"] == 0
    attempt = await database.create_attempt(window["id"], 1, "legacy-task")
    assert attempt["service_status"] == ""
    assert attempt["progress"] == 0
    await database.assign_legacy_jobs_with_attempts("http://islice-a.test/")
    assert (await database.get_job("job1"))["islice_base_url"] == "http://islice-a.test"


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
