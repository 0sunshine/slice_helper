from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from slice_helper.config import Settings
from slice_helper.media import (
    MediaError,
    MediaService,
    TimelineDiscontinuities,
    _select_dts_delta_threshold,
)
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
async def test_copy_cut_retries_with_accurate_seek_after_empty_fast_cut(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    media = MediaService(_test_settings(tmp_path))
    commands: list[tuple[str, ...]] = []
    probe_calls = 0

    async def fake_run(*args: str, timeout: float) -> tuple[str, str]:
        commands.append(args)
        if args[-1] != "-":
            Path(args[-1]).write_bytes(b"media")
        return "", ""

    async def fake_probe(_path: Path) -> MediaProbe:
        nonlocal probe_calls
        probe_calls += 1
        return MediaProbe(
            duration=0.04 if probe_calls == 1 else 4_042.9,
            format_name="mpegts",
            video_codec="h264",
            audio_codec="aac",
        )

    monkeypatch.setattr(media, "_run", fake_run)
    monkeypatch.setattr(media, "probe", fake_probe)

    async def fake_threshold(_source: Path) -> float:
        return 18.72

    monkeypatch.setattr(
        media, "_get_accurate_seek_dts_delta_threshold", fake_threshold
    )

    target = tmp_path / "window.ts"
    await media.cut(tmp_path / "source.ts", target, 53_557.1, 57_600.0, "copy")

    cut_commands = [command for command in commands if command[-1] != "-"]
    assert len(cut_commands) == 2
    assert cut_commands[0].index("-ss") < cut_commands[0].index("-i")
    assert cut_commands[1].index("-ss") > cut_commands[1].index("-i")
    threshold_index = cut_commands[1].index("-dts_delta_threshold")
    assert cut_commands[1][threshold_index + 1] == "18.720"
    assert threshold_index < cut_commands[1].index("-i")
    assert ("-c", "copy") in list(zip(cut_commands[1], cut_commands[1][1:]))
    assert "libx264" not in cut_commands[1]
    assert target.is_file()


@pytest.mark.asyncio
async def test_copy_cut_fails_after_repaired_audio_is_still_invalid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    media = MediaService(_test_settings(tmp_path))
    commands: list[tuple[str, ...]] = []

    async def fake_run(*args: str, timeout: float) -> tuple[str, str]:
        commands.append(args)
        if args[-1] == "-":
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
    with pytest.raises(MediaError, match="automatic video re-encoding is disabled"):
        await media.cut(tmp_path / "source.ts", target, 0, 4, "copy")

    cut_commands = [command for command in commands if command[-1] != "-"]
    assert len(cut_commands) == 2
    assert "libx264" not in cut_commands[1]
    assert not target.exists()
    assert not (tmp_path / "window.partial.ts").exists()


def test_dynamic_dts_threshold_preserves_forward_gaps_and_repairs_backtracks() -> None:
    threshold = _select_dts_delta_threshold(
        TimelineDiscontinuities(
            max_forward_gap=17.72,
            min_backward_jump=29_580.85,
        )
    )

    assert threshold == pytest.approx(18.72)


def test_dynamic_dts_threshold_keeps_ffmpeg_default_without_large_forward_gap() -> None:
    assert (
        _select_dts_delta_threshold(
            TimelineDiscontinuities(
                max_forward_gap=9.9,
                min_backward_jump=29_580.85,
            )
        )
        is None
    )


def test_dynamic_dts_threshold_rejects_ambiguous_timeline() -> None:
    with pytest.raises(MediaError, match="cannot be repaired safely"):
        _select_dts_delta_threshold(
            TimelineDiscontinuities(
                max_forward_gap=20.0,
                min_backward_jump=20.5,
            )
        )


def test_dynamic_threshold_repairs_huge_forward_jump_but_preserves_small_gaps() -> None:
    threshold = _select_dts_delta_threshold(
        TimelineDiscontinuities(
            max_forward_gap=54_535.23,
            min_backward_jump=95_443.68,
            max_preserved_forward_gap=23.124,
            min_forward_discontinuity=54_535.23,
        )
    )

    assert threshold == pytest.approx(24.2802)


@pytest.mark.asyncio
async def test_prepare_timeline_source_stream_copies_and_reuses_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    media = MediaService(_test_settings(tmp_path))
    source = tmp_path / "source.ts"
    source.write_bytes(b"source packets")
    original_probe = MediaProbe(
        duration=43_022.0,
        format_name="mpegts",
        video_codec="h264",
        audio_codec="aac",
    )
    repaired_probe = MediaProbe(
        duration=86_402.0,
        format_name="mpegts",
        video_codec="h264",
        audio_codec="aac",
    )
    monkeypatch.setattr(
        "slice_helper.media._timeline_preflight_is_suspicious",
        lambda _source, _duration: True,
    )
    scans = [
        TimelineDiscontinuities(
            max_forward_gap=54_535.23,
            min_backward_jump=95_443.68,
            max_preserved_forward_gap=23.124,
            min_forward_discontinuity=54_535.23,
        ),
        TimelineDiscontinuities(max_forward_gap=23.124),
    ]

    async def fake_scan(_source: Path) -> TimelineDiscontinuities:
        return scans.pop(0)

    commands: list[tuple[str, ...]] = []

    async def fake_run(*args: str, timeout: float) -> tuple[str, str]:
        commands.append(args)
        Path(args[-1]).write_bytes(b"repaired packets")
        return "", ""

    async def fake_probe(path: Path) -> MediaProbe:
        return repaired_probe if "repaired" in path.name else original_probe

    monkeypatch.setattr(media, "_scan_timeline_discontinuities", fake_scan)
    monkeypatch.setattr(media, "_run", fake_run)
    monkeypatch.setattr(media, "probe", fake_probe)

    prepared = await media.prepare_timeline_source(source, original_probe)
    reused = await media.prepare_timeline_source(source, original_probe)

    assert prepared.repaired is True
    assert prepared.path.is_file()
    assert prepared.probe.duration == 86_402.0
    assert reused.path == prepared.path
    assert len(commands) == 1
    command = commands[0]
    assert command[command.index("-dts_delta_threshold") + 1] == "24.280"
    assert ("-c", "copy") in list(zip(command, command[1:]))
    assert "libx264" not in command


@pytest.mark.asyncio
async def test_prepare_timeline_source_repairs_real_ffmpeg_pts_reset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        pytest.skip("FFmpeg is not installed")
    configured = _test_settings(tmp_path)
    configured = Settings(
        **{
            field: getattr(configured, field)
            for field in configured.__dataclass_fields__
            if field not in {"ffmpeg_path", "ffprobe_path", "ffmpeg_timeout_seconds"}
        },
        ffmpeg_path=ffmpeg,
        ffprobe_path=ffprobe,
        ffmpeg_timeout_seconds=60,
    )
    media = MediaService(configured)
    monkeypatch.setattr("slice_helper.media.TIMELINE_PREFLIGHT_READ_BYTES", 16 * 1024)
    monkeypatch.setattr("slice_helper.media.TIMELINE_PREFLIGHT_SAMPLE_SECONDS", 1.0)
    source = tmp_path / "discontinuous.ts"
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
        "-t",
        "9",
        "-c:v",
        "mpeg2video",
        "-b:v",
        "2M",
        "-g",
        "25",
        "-an",
        "-f",
        "mpegts",
        str(source),
        timeout=60,
    )

    def shift_timestamp(packet: bytearray, offset: int, seconds: int) -> None:
        value = (
            ((packet[offset] >> 1) & 0x07) << 30
            | packet[offset + 1] << 22
            | (packet[offset + 2] >> 1) << 15
            | packet[offset + 3] << 7
            | packet[offset + 4] >> 1
        )
        value = (value + seconds * 90_000) % (1 << 33)
        prefix = packet[offset] & 0xF0
        packet[offset] = prefix | ((value >> 30) & 0x07) << 1 | 1
        packet[offset + 1] = (value >> 22) & 0xFF
        packet[offset + 2] = ((value >> 15) & 0x7F) << 1 | 1
        packet[offset + 3] = (value >> 7) & 0xFF
        packet[offset + 4] = (value & 0x7F) << 1 | 1

    packets = bytearray(source.read_bytes())
    packet_count = len(packets) // 188
    for packet_index in range(packet_count // 3, packet_count * 2 // 3):
        start = packet_index * 188
        packet = packets[start : start + 188]
        if packet[0] != 0x47 or not (packet[1] & 0x40):
            continue
        adaptation_control = (packet[3] >> 4) & 0x03
        if adaptation_control not in (1, 3):
            continue
        payload = 4 + (1 + packet[4] if adaptation_control == 3 else 0)
        if payload + 19 > len(packet):
            continue
        if packet[payload : payload + 3] != b"\x00\x00\x01":
            continue
        flags = packet[payload + 7] & 0xC0
        if flags & 0x80:
            shift_timestamp(packet, payload + 9, 1000)
        if flags == 0xC0:
            shift_timestamp(packet, payload + 14, 1000)
        packets[start : start + 188] = packet
    source.write_bytes(packets)
    original = await media.probe(source)

    prepared = await media.prepare_timeline_source(source, original)

    assert prepared.repaired is True
    assert prepared.path != source
    assert 8.0 < prepared.probe.duration < 15.0
    assert prepared.probe.duration < original.duration
