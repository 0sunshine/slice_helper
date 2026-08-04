from __future__ import annotations

import re
import threading
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable


class TimeOcrError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class OcrText:
    text: str
    confidence: float


@dataclass(frozen=True, slots=True)
class TimeReference:
    source_start_time: datetime
    observed_time: datetime
    frame_offset_seconds: float
    matched_text: str
    confidence: float
    ocr_texts: tuple[OcrText, ...]


_TIMESTAMP_PATTERN = re.compile(
    r"(?<!\d)"
    r"(?P<year>20\d{2})\s*[-/.年]\s*"
    r"(?P<month>\d{1,2})\s*[-/.月]\s*"
    r"(?P<day>\d{1,2})\s*(?:日)?\s*[_T ]+\s*"
    r"(?P<hour>\d{1,2})\s*[:：]\s*"
    r"(?P<minute>\d{1,2})\s*[:：]\s*"
    r"(?P<second>\d{1,2})(?!\d)"
)

_engine = None
_engine_lock = threading.Lock()


def _normalized(value: str) -> str:
    return unicodedata.normalize("NFKC", value).strip()


def parse_time_reference(
    texts: Iterable[OcrText], *, frame_offset_seconds: float = 0.0
) -> TimeReference:
    detected = tuple(
        OcrText(_normalized(item.text), float(item.confidence))
        for item in texts
        if _normalized(item.text)
    )
    if not detected:
        raise TimeOcrError("OCR returned no text")

    candidates: list[tuple[datetime, str, float]] = []
    searchable: list[tuple[str, float]] = [
        (item.text, item.confidence) for item in detected
    ]
    searchable.extend(
        (f"{left.text} {right.text}", min(left.confidence, right.confidence))
        for left, right in zip(detected, detected[1:])
    )
    searchable.append((" ".join(item.text for item in detected), min(item.confidence for item in detected)))

    for text, confidence in searchable:
        for match in _TIMESTAMP_PATTERN.finditer(text):
            try:
                timestamp = datetime(
                    int(match.group("year")),
                    int(match.group("month")),
                    int(match.group("day")),
                    int(match.group("hour")),
                    int(match.group("minute")),
                    int(match.group("second")),
                )
            except ValueError:
                continue
            candidates.append((timestamp, match.group(0).strip(), confidence))

    if not candidates:
        rendered = " | ".join(item.text for item in detected)
        raise TimeOcrError(f"No complete timestamp was found in OCR text: {rendered}")

    observed_time, matched_text, confidence = max(candidates, key=lambda item: item[2])
    return TimeReference(
        source_start_time=observed_time - timedelta(seconds=frame_offset_seconds),
        observed_time=observed_time,
        frame_offset_seconds=frame_offset_seconds,
        matched_text=matched_text,
        confidence=confidence,
        ocr_texts=detected,
    )


def recognize_time_reference(
    frame_path: Path, *, frame_offset_seconds: float = 0.0
) -> TimeReference:
    global _engine

    with _engine_lock:
        if _engine is None:
            try:
                from rapidocr_onnxruntime import RapidOCR
            except ImportError as exc:
                raise TimeOcrError("rapidocr-onnxruntime is not installed") from exc
            _engine = RapidOCR()
        try:
            result, _ = _engine(str(frame_path))
        except Exception as exc:
            raise TimeOcrError(f"OCR inference failed: {exc}") from exc

    texts = [
        OcrText(text=str(item[1]), confidence=float(item[2]))
        for item in (result or [])
        if isinstance(item, (list, tuple)) and len(item) >= 3
    ]
    return parse_time_reference(texts, frame_offset_seconds=frame_offset_seconds)
