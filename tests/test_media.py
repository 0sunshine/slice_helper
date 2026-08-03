from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from slice_helper.config import Settings
from slice_helper.media import MediaService


@pytest.mark.asyncio
async def test_ffmpeg_copy_cut_with_synthetic_ts(tmp_path: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        pytest.skip("FFmpeg is not installed")
    configured = Settings(
        islice_base_url="http://islice.test",
        public_base_url="http://helper.test",
        host="127.0.0.1",
        port=8090,
        data_dir=tmp_path / "data",
        temp_dir=tmp_path / "temp",
        ffmpeg_path=ffmpeg,
        ffprobe_path=ffprobe,
        max_active_jobs=1,
        poll_interval_seconds=1,
        window_timeout_seconds=10,
        ffmpeg_timeout_seconds=60,
    )
    media = MediaService(configured)
    ready, message = await media.tools_ready()
    assert ready, message
    source = tmp_path / "source.ts"
    await media._run(
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "testsrc=size=320x180:rate=25",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=1000:sample_rate=48000",
        "-t",
        "8",
        "-c:v",
        "mpeg2video",
        "-g",
        "25",
        "-c:a",
        "mp2",
        "-f",
        "mpegts",
        str(source),
        timeout=60,
    )
    probe = await media.probe(source)
    assert probe.duration > 7
    target = tmp_path / "cut.ts"
    result = await media.cut(source, target, 2, 6, "copy")
    assert target.is_file()
    assert 2.5 < result.duration < 6
