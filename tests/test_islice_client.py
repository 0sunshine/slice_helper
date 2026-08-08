from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from slice_helper.config import Settings
from slice_helper.islice import (
    ISliceClient,
    ISliceConfigurationError,
    ISliceConflictError,
    ISlicePool,
)


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


def test_settings_parse_multiple_islice_urls(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(
        "ISLICE_BASE_URLS",
        "http://islice-a.test/, http://islice-b.test,http://islice-a.test",
    )
    configured = Settings.from_env(tmp_path)
    assert configured.islice_base_url == "http://islice-a.test"
    assert configured.configured_islice_urls == (
        "http://islice-a.test",
        "http://islice-b.test",
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


@pytest.mark.asyncio
async def test_delete_task_is_idempotent_for_missing_task(tmp_path: Path) -> None:
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        assert request.method == "POST"
        assert request.url.path == "/DeleteTask"
        return httpx.Response(404)

    client = ISliceClient(settings(tmp_path), transport=httpx.MockTransport(handler))
    assert await client.delete_task("task1") is False
    assert calls == ["/DeleteTask"]
    await client.close()


@pytest.mark.asyncio
async def test_pool_resolves_only_configured_instances(tmp_path: Path) -> None:
    configured = settings(tmp_path)
    configured = replace(
        configured,
        islice_base_urls=("http://islice-a.test/", "http://islice-b.test"),
    )
    pool = ISlicePool(configured)
    assert pool.urls == ("http://islice-a.test", "http://islice-b.test")
    assert pool.get_client("http://islice-b.test/").base_url == "http://islice-b.test"
    with pytest.raises(ISliceConfigurationError):
        pool.get_client("http://removed-islice.test")
    await pool.close()
