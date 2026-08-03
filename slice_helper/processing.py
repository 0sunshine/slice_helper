from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any


class SegmentValidationError(ValueError):
    pass


@dataclass(slots=True)
class ProcessedWindow:
    segments: list[dict[str, Any]]
    next_window_start: float | None
    handoff_start: float | None
    warning: str | None = None


def calculate_total_windows(
    duration: float, window_seconds: float, boundary_tolerance_seconds: float
) -> int:
    if duration <= 0 or window_seconds <= 0:
        raise ValueError("duration and window_seconds must be positive")
    adjusted_duration = max(0.0, duration - boundary_tolerance_seconds)
    return max(1, math.ceil(adjusted_duration / window_seconds))


def calculate_window_end(
    window_index: int,
    total_windows: int,
    source_duration: float,
    window_seconds: float,
) -> float:
    if window_index == total_windows - 1:
        return source_duration
    return min((window_index + 1) * window_seconds, source_duration)


def _number(value: Any, field: str, index: int) -> float:
    if isinstance(value, bool):
        raise SegmentValidationError(f"segment {index} {field} must be numeric")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise SegmentValidationError(f"segment {index} {field} must be numeric") from exc
    if not math.isfinite(result):
        raise SegmentValidationError(f"segment {index} {field} must be finite")
    return result


def _keywords(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [item.strip() for item in value.replace("，", ",").split(",") if item.strip()]
    return []


def _absolute(base: datetime | None, seconds: float) -> str | None:
    if base is None:
        return None
    return (base + timedelta(seconds=seconds)).isoformat()


def process_segments(
    raw_segments: Any,
    *,
    window_start: float,
    chunk_duration: float,
    is_final_window: bool,
    handoff_max_seconds: float,
    program_start_time: datetime | None,
) -> ProcessedWindow:
    if not isinstance(raw_segments, list) or not raw_segments:
        raise SegmentValidationError("iSlice returned no segments")

    normalized: list[dict[str, Any]] = []
    for source_index, raw in enumerate(raw_segments):
        if not isinstance(raw, dict):
            raise SegmentValidationError(f"segment {source_index} must be an object")
        start = _number(raw.get("startTime"), "startTime", source_index)
        end = _number(raw.get("endTime"), "endTime", source_index)
        if start < -1.0:
            raise SegmentValidationError(f"segment {source_index} starts before the chunk")
        if end <= start:
            raise SegmentValidationError(f"segment {source_index} has a non-positive duration")
        if end > chunk_duration + 30.0:
            raise SegmentValidationError(f"segment {source_index} exceeds the chunk by more than 30 seconds")
        start = max(0.0, start)
        global_start = window_start + start
        global_end = window_start + end
        normalized.append(
            {
                "source_index": source_index,
                "accepted": 1,
                "reason": "",
                "local_start": start,
                "local_end": end,
                "global_start": global_start,
                "global_end": global_end,
                "absolute_start": _absolute(program_start_time, global_start),
                "absolute_end": _absolute(program_start_time, global_end),
                "title": str(raw.get("title") or ""),
                "topic": str(raw.get("topic") or ""),
                "keywords_json": json.dumps(_keywords(raw.get("keywords")), ensure_ascii=False),
                "summary": str(raw.get("summary") or ""),
                "segment_url": str(raw.get("segmentUrl") or ""),
                "cover_img_url": str(raw.get("coverImgUrl") or ""),
                "raw_json": json.dumps(raw, ensure_ascii=False, separators=(",", ":")),
            }
        )

    normalized.sort(key=lambda item: (item["local_start"], item["local_end"], item["source_index"]))
    for previous, current in zip(normalized, normalized[1:]):
        if current["local_start"] < previous["local_end"] - 1.0:
            raise SegmentValidationError(
                f"segments {previous['source_index']} and {current['source_index']} overlap"
            )

    if is_final_window:
        return ProcessedWindow(normalized, None, None)

    tail = normalized[-1]
    tail_duration = tail["local_end"] - tail["local_start"]
    if tail_duration <= handoff_max_seconds:
        tail["accepted"] = 0
        tail["reason"] = "handoff"
        handoff_start = tail["global_start"]
        return ProcessedWindow(normalized, handoff_start, handoff_start)

    warning = (
        f"Window tail is {tail_duration:.2f}s (> {handoff_max_seconds:.2f}s); "
        "the tail was kept and the next window starts at the nominal boundary."
    )
    tail["reason"] = "long_tail_kept"
    return ProcessedWindow(normalized, window_start + chunk_duration, None, warning)
