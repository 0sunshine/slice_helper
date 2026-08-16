from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import os
import re
import shutil
from pathlib import Path

from .config import Settings
from .models import CutMode, MediaProbe
from .time_ocr import TimeOcrError, TimeReference, recognize_time_reference


logger = logging.getLogger(__name__)


class MediaError(RuntimeError):
    pass


class FastSeekError(MediaError):
    """The input-side seek produced an empty or wrongly sized chunk."""


class MediaService:
    def __init__(self, settings: Settings):
        self.settings = settings

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
            try:
                try:
                    result = await render()
                except FastSeekError as fast_seek_error:
                    accurate_seek = True
                    logger.warning(
                        "Fast chunk seek failed; retrying with sequential accurate seek: %s",
                        fast_seek_error,
                    )
                    result = await render(accurate_seek=True)
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
