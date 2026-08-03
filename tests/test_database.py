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
    assert (await database.get_job("job1"))["status"] == "queued"

    window = await database.upsert_window("job1", 0, 0, 3600)
    same_window = await database.upsert_window("job1", 0, 99, 999)
    assert window["id"] == same_window["id"]
    assert same_window["requested_start"] == 0
