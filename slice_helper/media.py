from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import logging
import math
import os
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from .config import Settings
from .models import CutMode, MediaProbe
from .time_ocr import TimeOcrError, TimeReference, recognize_time_reference


logger = logging.getLogger(__name__)


FFMPEG_DEFAULT_DTS_DELTA_THRESHOLD_SECONDS = 10.0
TIMELINE_THRESHOLD_MARGIN_SECONDS = 1.0
MAX_PRESERVED_SOURCE_GAP_SECONDS = 300.0
TIMELINE_PROFILE_VERSION = 1
TIMELINE_PREFLIGHT_SAMPLE_SECONDS = 300.0
TIMELINE_PREFLIGHT_READ_BYTES = 512 * 1024


class MediaError(RuntimeError):
    pass


class FastSeekError(MediaError):
    """The input-side seek produced an empty or wrongly sized chunk."""


@dataclass(frozen=True, slots=True)
class TimelineDiscontinuities:
    max_forward_gap: float = 0.0
    min_backward_jump: float | None = None
    max_preserved_forward_gap: float = 0.0
    min_forward_discontinuity: float | None = None

    @property
    def requires_repair(self) -> bool:
        return (
            self.min_forward_discontinuity is not None
            or self.min_backward_jump is not None
        )


@dataclass(frozen=True, slots=True)
class PreparedMedia:
    path: Path
    probe: MediaProbe
    repaired: bool = False
    original_duration: float = 0.0
    dts_delta_threshold: float | None = None


def _select_dts_delta_threshold(discontinuities: TimelineDiscontinuities) -> float | None:
    """Keep real forward gaps while still allowing FFmpeg to repair backward jumps."""
    forward_gap = discontinuities.max_preserved_forward_gap
    # Keep compatibility with callers/tests that only supply the legacy maximum.
    if (
        forward_gap <= 0
        and discontinuities.min_forward_discontinuity is None
        and discontinuities.max_forward_gap <= MAX_PRESERVED_SOURCE_GAP_SECONDS
    ):
        forward_gap = discontinuities.max_forward_gap
    if forward_gap <= FFMPEG_DEFAULT_DTS_DELTA_THRESHOLD_SECONDS:
        return None
    threshold = forward_gap + max(
        TIMELINE_THRESHOLD_MARGIN_SECONDS,
        forward_gap * 0.05,
    )
    repair_jumps = [
        value
        for value in (
            discontinuities.min_forward_discontinuity,
            discontinuities.min_backward_jump,
        )
        if value is not None
    ]
    smallest_repair_jump = min(repair_jumps) if repair_jumps else None
    if smallest_repair_jump is not None and threshold >= smallest_repair_jump:
        raise MediaError(
            "Source timeline cannot be repaired safely: the largest preserved "
            f"forward gap is {forward_gap:.2f}s and the smallest timestamp "
            f"discontinuity is {smallest_repair_jump:.2f}s"
        )
    return threshold


def _decode_mpegts_pts(data: bytes) -> float | None:
    if len(data) < 5:
        return None
    value = (
        ((data[0] >> 1) & 0x07) << 30
        | data[1] << 22
        | (data[2] >> 1) << 15
        | data[3] << 7
        | data[4] >> 1
    )
    return value / 90000.0


def _mpegts_packet_alignment(data: bytes) -> tuple[int, int] | None:
    for packet_size in (188, 192, 204):
        for offset in range(min(packet_size, len(data))):
            if all(
                offset + packet_size * index < len(data)
                and data[offset + packet_size * index] == 0x47
                for index in range(5)
            ):
                return packet_size, offset
    return None


def _first_video_pts(data: bytes) -> float | None:
    alignment = _mpegts_packet_alignment(data)
    if alignment is None:
        return None
    packet_size, offset = alignment
    for start in range(offset, len(data) - packet_size + 1, packet_size):
        packet = data[start : start + packet_size]
        if packet[0] != 0x47 or not (packet[1] & 0x40):
            continue
        adaptation_control = (packet[3] >> 4) & 0x03
        if adaptation_control not in (1, 3):
            continue
        payload_offset = 4
        if adaptation_control == 3:
            payload_offset += 1 + packet[4]
        if payload_offset + 14 > len(packet):
            continue
        pes = packet[payload_offset:]
        if pes[:3] != b"\x00\x00\x01" or not 0xE0 <= pes[3] <= 0xEF:
            continue
        if not (pes[7] & 0x80):
            continue
        return _decode_mpegts_pts(pes[9:14])
    return None


def _timeline_preflight_is_suspicious(source: Path, duration: float) -> bool:
    """Sample PTS by physical file order without relying on FFmpeg seeking."""
    size = source.stat().st_size
    if size <= 0:
        return False
    sample_count = max(
        24,
        min(360, int(math.ceil(max(duration, 1.0) / TIMELINE_PREFLIGHT_SAMPLE_SECONDS))),
    )
    positions = sorted(
        {
            min(max(0, size - TIMELINE_PREFLIGHT_READ_BYTES), int(size * index / sample_count))
            for index in range(sample_count + 1)
        }
    )
    samples: list[float] = []
    with source.open("rb", buffering=0) as handle:
        for position in positions:
            handle.seek(position)
            timestamp = _first_video_pts(handle.read(TIMELINE_PREFLIGHT_READ_BYTES))
            if timestamp is not None:
                samples.append(timestamp)
    # A valid ffprobe result plus no directly readable PES timestamps is not enough
    # evidence to rewrite a source. The normal cut validation remains in force.
    if len(samples) < 2:
        return False
    for previous, current in zip(samples, samples[1:]):
        delta = current - previous
        normal_wrap = previous > 90000.0 and current < 10000.0
        if delta > 5000.0 or (delta < -900.0 and not normal_wrap):
            return True
    return False


class MediaService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._timeline_threshold_cache: dict[tuple[str, int, int], float | None] = {}
        self._timeline_threshold_locks: dict[tuple[str, int, int], asyncio.Lock] = {}
        self._timeline_prepare_locks: dict[str, asyncio.Lock] = {}
        self._timeline_prepare_gate = asyncio.Semaphore(1)

    async def _scan_timeline_discontinuities(
        self, source: Path
    ) -> TimelineDiscontinuities:
        command = [
            self.settings.ffprobe_path,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "packet=stream_index,pts_time,dts_time",
            "-of",
            "compact=p=0:nk=0",
            str(source),
        ]
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise MediaError(f"Executable not found: {command[0]}") from exc
        assert process.stdout is not None
        assert process.stderr is not None

        async def scan_stdout() -> TimelineDiscontinuities:
            previous_by_stream: dict[str, float] = {}
            max_forward_gap = 0.0
            max_preserved_forward_gap = 0.0
            min_forward_discontinuity: float | None = None
            min_backward_jump: float | None = None
            async for raw_line in process.stdout:
                fields: dict[str, str] = {}
                for item in raw_line.decode("utf-8", errors="replace").strip().split("|"):
                    key, separator, value = item.partition("=")
                    if separator:
                        fields[key] = value
                stream_index = fields.get("stream_index")
                raw_timestamp = fields.get("dts_time")
                if not raw_timestamp or raw_timestamp == "N/A":
                    raw_timestamp = fields.get("pts_time")
                if not stream_index or not raw_timestamp or raw_timestamp == "N/A":
                    continue
                try:
                    timestamp = float(raw_timestamp)
                except ValueError:
                    continue
                previous = previous_by_stream.get(stream_index)
                previous_by_stream[stream_index] = timestamp
                if previous is None:
                    continue
                delta = timestamp - previous
                if delta > FFMPEG_DEFAULT_DTS_DELTA_THRESHOLD_SECONDS:
                    max_forward_gap = max(max_forward_gap, delta)
                    if delta <= MAX_PRESERVED_SOURCE_GAP_SECONDS:
                        max_preserved_forward_gap = max(
                            max_preserved_forward_gap, delta
                        )
                    else:
                        min_forward_discontinuity = (
                            delta
                            if min_forward_discontinuity is None
                            else min(min_forward_discontinuity, delta)
                        )
                elif delta < -FFMPEG_DEFAULT_DTS_DELTA_THRESHOLD_SECONDS:
                    jump = abs(delta)
                    min_backward_jump = (
                        jump
                        if min_backward_jump is None
                        else min(min_backward_jump, jump)
                    )
            return TimelineDiscontinuities(
                max_forward_gap=max_forward_gap,
                min_backward_jump=min_backward_jump,
                max_preserved_forward_gap=max_preserved_forward_gap,
                min_forward_discontinuity=min_forward_discontinuity,
            )

        stderr_task = asyncio.create_task(process.stderr.read())
        try:
            discontinuities = await asyncio.wait_for(
                scan_stdout(), timeout=self.settings.ffmpeg_timeout_seconds
            )
            return_code = await process.wait()
            stderr = (await stderr_task).decode("utf-8", errors="replace")
        except TimeoutError as exc:
            process.kill()
            await process.wait()
            await stderr_task
            raise MediaError(
                "Source timeline analysis timed out after "
                f"{self.settings.ffmpeg_timeout_seconds:.0f}s"
            ) from exc
        if return_code != 0:
            message = stderr.strip().splitlines()[-1] if stderr.strip() else "unknown error"
            raise MediaError(f"ffprobe timeline analysis failed: {message}")
        return discontinuities

    async def _get_accurate_seek_dts_delta_threshold(
        self, source: Path
    ) -> float | None:
        try:
            stat = source.stat()
        except OSError as exc:
            logger.warning("Could not inspect source timeline metadata: %s", exc)
            return None
        cache_key = (str(source.resolve()), stat.st_size, stat.st_mtime_ns)
        if cache_key in self._timeline_threshold_cache:
            return self._timeline_threshold_cache[cache_key]
        lock = self._timeline_threshold_locks.setdefault(cache_key, asyncio.Lock())
        async with lock:
            if cache_key in self._timeline_threshold_cache:
                return self._timeline_threshold_cache[cache_key]
            discontinuities = await self._scan_timeline_discontinuities(source)
            threshold = _select_dts_delta_threshold(discontinuities)
            self._timeline_threshold_cache[cache_key] = threshold
            logger.info(
                "Source timeline analysis completed for %s: max forward gap %.2fs, "
                "minimum backward jump %s, accurate-seek DTS threshold %s",
                source,
                discontinuities.max_forward_gap,
                (
                    f"{discontinuities.min_backward_jump:.2f}s"
                    if discontinuities.min_backward_jump is not None
                    else "none"
                ),
                f"{threshold:.2f}s" if threshold is not None else "default",
            )
            return threshold

    async def _run(self, *args: str, timeout: float) -> tuple[str, str]:
        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise MediaError(f"Executable not found: {args[0]}") from exc
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except TimeoutError as exc:
            process.kill()
            await process.communicate()
            raise MediaError(f"Command timed out after {timeout:.0f}s") from exc
        stdout_text = stdout.decode("utf-8", errors="replace")
        stderr_text = stderr.decode("utf-8", errors="replace")
        if process.returncode != 0:
            message = stderr_text.strip().splitlines()[-1] if stderr_text.strip() else "unknown error"
            raise MediaError(f"{Path(args[0]).name} failed: {message}")
        return stdout_text, stderr_text

    async def probe(self, path: Path) -> MediaProbe:
        stdout, _ = await self._run(
            self.settings.ffprobe_path,
            "-v",
            "error",
            "-show_entries",
            "format=duration,format_name:stream=index,codec_type,codec_name",
            "-of",
            "json",
            str(path),
            timeout=120.0,
        )
        try:
            payload = json.loads(stdout)
            duration = float(payload["format"]["duration"])
            format_name = str(payload["format"].get("format_name") or "")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise MediaError("FFprobe did not return a valid media duration") from exc
        if duration <= 0:
            raise MediaError("Media duration must be positive")
        streams = payload.get("streams") or []
        video = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
        audio = next((stream for stream in streams if stream.get("codec_type") == "audio"), None)
        if video is None:
            raise MediaError("No video stream was found")
        return MediaProbe(
            duration=duration,
            format_name=format_name,
            video_codec=str(video.get("codec_name") or "unknown"),
            audio_codec=str(audio.get("codec_name")) if audio else None,
        )

    def _timeline_profile_paths(self, source: Path) -> tuple[str, Path, Path]:
        stat = source.stat()
        fingerprint = hashlib.sha256(
            f"{TIMELINE_PROFILE_VERSION}\0{source.resolve()}\0{stat.st_size}\0{stat.st_mtime_ns}".encode(
                "utf-8", errors="surrogatepass"
            )
        ).hexdigest()
        directory = self.settings.temp_dir / "timeline-repair" / fingerprint
        return fingerprint, directory / "profile.json", directory / "repaired.ts"

    @staticmethod
    def _read_timeline_profile(path: Path) -> dict | None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return None
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def _write_timeline_profile(path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)

    @staticmethod
    def _timeline_profile_matches(
        profile: dict, source: Path, source_size: int, source_mtime_ns: int
    ) -> bool:
        return (
            int(profile.get("version") or 0) == TIMELINE_PROFILE_VERSION
            and str(profile.get("sourcePath") or "") == str(source.resolve())
            and int(profile.get("sourceSize") or -1) == source_size
            and int(profile.get("sourceMtimeNs") or -1) == source_mtime_ns
            and str(profile.get("status") or "") in {"normal", "repaired"}
        )

    async def prepare_timeline_source(
        self,
        source: Path,
        original_probe: MediaProbe | None = None,
    ) -> PreparedMedia:
        """Return a stable source for new jobs without changing the original file."""
        source = source.resolve()
        try:
            stat = source.stat()
        except OSError as exc:
            raise MediaError(f"Could not inspect source file: {exc}") from exc
        probe = original_probe or await self.probe(source)
        fingerprint, profile_path, repaired_path = self._timeline_profile_paths(source)
        lock = self._timeline_prepare_locks.setdefault(fingerprint, asyncio.Lock())
        async with lock:
            profile = await asyncio.to_thread(
                self._read_timeline_profile, profile_path
            )
            if profile and self._timeline_profile_matches(
                profile, source, stat.st_size, stat.st_mtime_ns
            ):
                if profile["status"] == "normal":
                    return PreparedMedia(
                        path=source,
                        probe=probe,
                        repaired=False,
                        original_duration=probe.duration,
                    )
                if repaired_path.is_file():
                    try:
                        repaired_probe = await self.probe(repaired_path)
                    except MediaError as exc:
                        logger.warning(
                            "Cached repaired timeline source is invalid; rebuilding it: %s",
                            exc,
                        )
                    else:
                        raw_threshold = profile.get("dtsDeltaThreshold")
                        return PreparedMedia(
                            path=repaired_path,
                            probe=repaired_probe,
                            repaired=True,
                            original_duration=float(
                                profile.get("originalDuration") or probe.duration
                            ),
                            dts_delta_threshold=(
                                float(raw_threshold)
                                if raw_threshold is not None
                                else None
                            ),
                        )

            async with self._timeline_prepare_gate:
                suspicious = await asyncio.to_thread(
                    _timeline_preflight_is_suspicious, source, probe.duration
                )
                if not suspicious:
                    payload = {
                        "version": TIMELINE_PROFILE_VERSION,
                        "status": "normal",
                        "sourcePath": str(source),
                        "sourceSize": stat.st_size,
                        "sourceMtimeNs": stat.st_mtime_ns,
                        "originalDuration": probe.duration,
                        "effectiveDuration": probe.duration,
                        "updatedAt": time.time(),
                    }
                    await asyncio.to_thread(
                        self._write_timeline_profile, profile_path, payload
                    )
                    return PreparedMedia(
                        path=source,
                        probe=probe,
                        repaired=False,
                        original_duration=probe.duration,
                    )

                discontinuities = await self._scan_timeline_discontinuities(source)
                if not discontinuities.requires_repair:
                    payload = {
                        "version": TIMELINE_PROFILE_VERSION,
                        "status": "normal",
                        "sourcePath": str(source),
                        "sourceSize": stat.st_size,
                        "sourceMtimeNs": stat.st_mtime_ns,
                        "originalDuration": probe.duration,
                        "effectiveDuration": probe.duration,
                        "updatedAt": time.time(),
                    }
                    await asyncio.to_thread(
                        self._write_timeline_profile, profile_path, payload
                    )
                    return PreparedMedia(
                        path=source,
                        probe=probe,
                        repaired=False,
                        original_duration=probe.duration,
                    )

                threshold = _select_dts_delta_threshold(discontinuities)
                repaired_path.parent.mkdir(parents=True, exist_ok=True)
                free_bytes = shutil.disk_usage(repaired_path.parent).free
                required_bytes = int(stat.st_size * 1.10) + 1024 * 1024 * 1024
                if free_bytes < required_bytes:
                    raise MediaError(
                        "Not enough temporary disk space to repair the source "
                        f"timeline: need {required_bytes} bytes, have {free_bytes}"
                    )
                partial = repaired_path.with_suffix(".partial.ts")
                partial.unlink(missing_ok=True)
                command = [
                    self.settings.ffmpeg_path,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-fflags",
                    "+discardcorrupt",
                    "-err_detect",
                    "ignore_err",
                ]
                if threshold is not None:
                    command.extend(["-dts_delta_threshold", f"{threshold:.3f}"])
                command.extend(
                    [
                        "-i",
                        str(source),
                        "-map",
                        "0:v:0",
                        "-map",
                        "0:a:0?",
                        "-c",
                        "copy",
                        "-f",
                        "mpegts",
                        str(partial),
                    ]
                )
                try:
                    await self._run(
                        *command, timeout=self.settings.ffmpeg_timeout_seconds
                    )
                    repaired_probe = await self.probe(partial)
                    repaired_discontinuities = (
                        await self._scan_timeline_discontinuities(partial)
                    )
                    if repaired_discontinuities.requires_repair:
                        raise MediaError(
                            "Timeline repair output still contains unsafe timestamp "
                            "discontinuities"
                        )
                    os.replace(partial, repaired_path)
                except Exception:
                    partial.unlink(missing_ok=True)
                    raise

                payload = {
                    "version": TIMELINE_PROFILE_VERSION,
                    "status": "repaired",
                    "sourcePath": str(source),
                    "sourceSize": stat.st_size,
                    "sourceMtimeNs": stat.st_mtime_ns,
                    "originalDuration": probe.duration,
                    "effectiveDuration": repaired_probe.duration,
                    "dtsDeltaThreshold": threshold,
                    "maxForwardGap": discontinuities.max_forward_gap,
                    "maxPreservedForwardGap": (
                        discontinuities.max_preserved_forward_gap
                    ),
                    "minForwardDiscontinuity": (
                        discontinuities.min_forward_discontinuity
                    ),
                    "minBackwardJump": discontinuities.min_backward_jump,
                    "updatedAt": time.time(),
                }
                await asyncio.to_thread(
                    self._write_timeline_profile, profile_path, payload
                )
                logger.warning(
                    "Prepared repaired timeline source %s from %s: duration %.2fs -> %.2fs",
                    repaired_path,
                    source,
                    probe.duration,
                    repaired_probe.duration,
                )
                return PreparedMedia(
                    path=repaired_path,
                    probe=repaired_probe,
                    repaired=True,
                    original_duration=probe.duration,
                    dts_delta_threshold=threshold,
                )

    async def cut(
        self,
        source: Path,
        target: Path,
        start: float,
        end: float,
        mode: CutMode | str,
    ) -> MediaProbe:
        duration = end - start
        if start < 0 or duration <= 0:
            raise MediaError("Invalid cut range")
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(f"{target.stem}.partial{target.suffix}")
        partial.unlink(missing_ok=True)

        async def render(
            *,
            repair_audio: bool = False,
            validate_aac_bitstream: bool = True,
            accurate_seek: bool = False,
            dts_delta_threshold: float | None = None,
        ) -> MediaProbe:
            partial.unlink(missing_ok=True)
            command = [
                self.settings.ffmpeg_path,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-fflags",
                "+discardcorrupt",
                "-err_detect",
                "ignore_err",
            ]
            if accurate_seek and dts_delta_threshold is not None:
                command.extend(
                    ["-dts_delta_threshold", f"{dts_delta_threshold:.3f}"]
                )
            if not accurate_seek:
                command.extend(["-ss", f"{start:.3f}"])
            command.extend(["-i", str(source)])
            if accurate_seek:
                # MPEG-TS PTS wraps roughly every 26.5 hours. Input-side
                # seeking can return an empty chunk when a long recording
                # crosses that boundary. Output-side seeking scans the
                # timeline in order and remains correct across the wrap.
                command.extend(["-ss", f"{start:.3f}"])
            command.extend(
                [
                    "-t",
                    f"{duration:.3f}",
                    "-map",
                    "0:v:0",
                    "-map",
                    "0:a:0?",
                ]
            )
            if str(mode) == CutMode.TRANSCODE.value:
                command.extend(
                    [
                        "-c:v",
                        "libx264",
                        "-preset",
                        "veryfast",
                        "-crf",
                        "20",
                        "-c:a",
                        "aac",
                        "-b:a",
                        "128k",
                    ]
                )
            elif repair_audio:
                command.extend(
                    [
                        "-c:v",
                        "copy",
                        "-c:a",
                        "aac",
                        "-b:a",
                        "128k",
                    ]
                )
            else:
                command.extend(["-c", "copy"])
            command.extend(["-f", "mpegts", str(partial)])

            await self._run(*command, timeout=self.settings.ffmpeg_timeout_seconds)
            try:
                result = await self.probe(partial)
            except MediaError as exc:
                if not accurate_seek:
                    raise FastSeekError(str(exc)) from exc
                raise
            if abs(result.duration - duration) > 60.0:
                error = (
                    "Cut duration differs by more than 60s: "
                    f"expected {duration:.2f}, got {result.duration:.2f}"
                )
                if not accurate_seek:
                    raise FastSeekError(error)
                raise MediaError(error)
            await self.validate_for_islice(
                partial, result, check_aac_bitstream=validate_aac_bitstream
            )
            return result

        try:
            accurate_seek = False
            dts_delta_threshold: float | None = None
            try:
                try:
                    result = await render()
                except FastSeekError as fast_seek_error:
                    accurate_seek = True
                    dts_delta_threshold = (
                        await self._get_accurate_seek_dts_delta_threshold(source)
                    )
                    logger.warning(
                        "Fast chunk seek failed; retrying with sequential accurate seek: %s",
                        fast_seek_error,
                    )
                    result = await render(
                        accurate_seek=True,
                        dts_delta_threshold=dts_delta_threshold,
                    )
            except MediaError as initial_error:
                if str(mode) != CutMode.COPY.value:
                    raise
                logger.warning(
                    "Copied chunk failed downstream validation; retrying with copied "
                    "video and repaired AAC audio: %s",
                    initial_error,
                )
                try:
                    # The repaired output is already freshly encoded AAC in an
                    # MPEG-TS container. Do not apply the MP4-oriented
                    # aac_adtstoasc probe to it; that filter can reject valid
                    # repaired TS packets with "Invalid argument".
                    result = await render(
                        repair_audio=True,
                        validate_aac_bitstream=False,
                        accurate_seek=accurate_seek,
                        dts_delta_threshold=dts_delta_threshold,
                    )
                except MediaError as repair_error:
                    raise MediaError(
                        "Chunk failed validation after audio repair; "
                        "automatic video re-encoding is disabled: "
                        f"{repair_error}"
                    ) from repair_error
            os.replace(partial, target)
            return await self.probe(target)
        except Exception:
            partial.unlink(missing_ok=True)
            raise

    async def validate_for_islice(
        self,
        path: Path,
        probe: MediaProbe | None = None,
        *,
        check_aac_bitstream: bool = True,
    ) -> None:
        media_probe = probe or await self.probe(path)
        command = [
            self.settings.ffmpeg_path,
            "-hide_banner",
            "-loglevel",
            "error",
            "-fflags",
            "+discardcorrupt",
            "-err_detect",
            "ignore_err",
            "-i",
            str(path),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            "-c",
            "copy",
        ]
        # iSlice writes MP4 segments. Scanning every AAC packet through the same
        # ADTS conversion catches malformed packets that metadata-only ffprobe misses.
        if check_aac_bitstream and media_probe.audio_codec == "aac":
            command.extend(["-bsf:a", "aac_adtstoasc"])
        command.extend(["-f", "null", "-"])
        try:
            await self._run(*command, timeout=self.settings.ffmpeg_timeout_seconds)
        except MediaError as exc:
            raise MediaError(f"Chunk is not safe for iSlice segmentation: {exc}") from exc

    async def detect_time_reference(
        self, source: Path, frame_path: Path, *, frame_offset_seconds: float = 0.0
    ) -> TimeReference:
        if frame_offset_seconds < 0:
            raise MediaError("OCR frame offset must not be negative")
        frame_path.parent.mkdir(parents=True, exist_ok=True)
        partial = frame_path.with_name(f"{frame_path.stem}.partial{frame_path.suffix}")
        partial.unlink(missing_ok=True)
        command = [
            self.settings.ffmpeg_path,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            f"{frame_offset_seconds:.3f}",
            "-i",
            str(source),
            "-map",
            "0:v:0",
            "-frames:v",
            "1",
            str(partial),
        ]
        try:
            await self._run(*command, timeout=120.0)
            if not partial.is_file() or partial.stat().st_size == 0:
                raise MediaError("FFmpeg did not create the OCR reference frame")
            os.replace(partial, frame_path)
            return await asyncio.to_thread(
                recognize_time_reference,
                frame_path,
                frame_offset_seconds=frame_offset_seconds,
            )
        except TimeOcrError as exc:
            raise MediaError(str(exc)) from exc
        finally:
            partial.unlink(missing_ok=True)

    @staticmethod
    def ocr_ready() -> tuple[bool, str]:
        if importlib.util.find_spec("rapidocr_onnxruntime") is None:
            return False, "rapidocr-onnxruntime is not installed"
        return True, "rapidocr-onnxruntime available"

    async def tools_ready(self) -> tuple[bool, str]:
        missing: list[str] = []
        for value in (self.settings.ffmpeg_path, self.settings.ffprobe_path):
            path = Path(value)
            if path.is_absolute():
                exists = path.is_file()
            else:
                exists = shutil.which(value) is not None
            if not exists:
                missing.append(value)
        if missing:
            return False, f"Missing executable(s): {', '.join(missing)}"
        versions: list[str] = []
        for executable in (self.settings.ffmpeg_path, self.settings.ffprobe_path):
            try:
                stdout, _ = await self._run(executable, "-version", timeout=10.0)
            except MediaError as exc:
                return False, str(exc)
            first_line = stdout.splitlines()[0] if stdout.splitlines() else ""
            match = re.search(r"\bversion\s+(\d+)", first_line)
            if match and int(match.group(1)) < 6:
                return False, f"{Path(executable).name} 6 or newer is required"
            versions.append(first_line or f"{Path(executable).name} ok")
        return True, "; ".join(versions)
