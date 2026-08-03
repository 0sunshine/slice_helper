from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from slice_helper.config import Settings
from slice_helper.islice import ISliceClient, ISliceConflictError


def settings(tmp_path: Path) -> Settings:
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


@pytest.mark.asyncio
async def test_ensure_task_attaches_to_matching_existing_task(tmp_path: Path) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/GetTaskInfo":
            return httpx.Response(
                200,
                json={"taskInfo": {"taskId": "task1", "videoPath": "http://helper/chunk.ts", "status": "processing"}, "segments": []},
            )
        raise AssertionError("CreateTask should not be called")

    client = ISliceClient(settings(tmp_path), transport=httpx.MockTransport(handler))
    result = await client.ensure_task(
        "task1", {"taskId": "task1", "videoPath": "http://helper/chunk.ts"}
    )
    assert result["taskInfo"]["status"] == "processing"
    await client.close()


@pytest.mark.asyncio
async def test_ensure_task_rejects_conflicting_existing_task(tmp_path: Path) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"taskInfo": {"taskId": "task1", "videoPath": "http://other/file.ts", "status": "processing"}, "segments": []},
        )

    client = ISliceClient(settings(tmp_path), transport=httpx.MockTransport(handler))
    with pytest.raises(ISliceConflictError):
        await client.ensure_task(
            "task1", {"taskId": "task1", "videoPath": "http://helper/chunk.ts"}
        )
    await client.close()
