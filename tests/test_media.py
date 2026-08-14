from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from slice_helper.config import Settings
from slice_helper.media import MediaError, MediaService
from slice_helper.models import MediaProbe


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


def _test_settings(tmp_path: Path) -> Settings:
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
        poll_interval_seconds=1,
        window_timeout_seconds=10,
        ffmpeg_timeout_seconds=60,
    )


@pytest.mark.asyncio
async def test_copy_cut_repairs_only_audio_after_validation_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    media = MediaService(_test_settings(tmp_path))
    commands: list[tuple[str, ...]] = []
    validation_calls = 0

    async def fake_run(*args: str, timeout: float) -> tuple[str, str]:
        nonlocal validation_calls
        commands.append(args)
        if args[-1] == "-":
            validation_calls += 1
            if validation_calls == 1:
                raise MediaError("malformed ADTS packet")
        else:
            Path(args[-1]).write_bytes(b"media")
        return "", ""

    async def fake_probe(_path: Path) -> MediaProbe:
        return MediaProbe(
            duration=4.0,
            format_name="mpegts",
            video_codec="h264",
            audio_codec="aac",
        )

    monkeypatch.setattr(media, "_run", fake_run)
    monkeypatch.setattr(media, "probe", fake_probe)

    target = tmp_path / "window.ts"
    await media.cut(tmp_path / "source.ts", target, 0, 4, "copy")

    cut_commands = [command for command in commands if command[-1] != "-"]
    assert len(cut_commands) == 2
    assert ("-c", "copy") in list(zip(cut_commands[0], cut_commands[0][1:]))
    assert ("-c:v", "copy") in list(zip(cut_commands[1], cut_commands[1][1:]))
    assert ("-c:a", "aac") in list(zip(cut_commands[1], cut_commands[1][1:]))
    assert "libx264" not in cut_commands[1]
    assert validation_calls == 2
    assert target.is_file()


@pytest.mark.asyncio
async def test_copy_cut_reencodes_video_when_audio_repair_is_still_invalid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    media = MediaService(_test_settings(tmp_path))
    commands: list[tuple[str, ...]] = []
    validation_calls = 0

    async def fake_run(*args: str, timeout: float) -> tuple[str, str]:
        nonlocal validation_calls
        commands.append(args)
        if args[-1] == "-":
            validation_calls += 1
            if validation_calls < 3:
                raise MediaError("still invalid")
        Path(args[-1]).write_bytes(b"media")
        return "", ""

    async def fake_probe(_path: Path) -> MediaProbe:
        return MediaProbe(
            duration=4.0,
            format_name="mpegts",
            video_codec="h264",
            audio_codec="aac",
        )

    monkeypatch.setattr(media, "_run", fake_run)
    monkeypatch.setattr(media, "probe", fake_probe)

    target = tmp_path / "window.ts"
    await media.cut(tmp_path / "source.ts", target, 0, 4, "copy")

    cut_commands = [command for command in commands if command[-1] != "-"]
    assert len(cut_commands) == 3
    assert "libx264" not in cut_commands[1]
    assert "libx264" in cut_commands[2]
    assert target.exists()
    assert not (tmp_path / "window.partial.ts").exists()
