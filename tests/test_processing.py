from __future__ import annotations

from datetime import datetime

import pytest

from slice_helper.processing import (
    SegmentValidationError,
    calculate_total_windows,
    calculate_window_end,
    process_segments,
)


def segment(start: float, end: float, title: str = "item") -> dict:
    return {
        "startTime": start,
        "endTime": end,
        "title": title,
        "topic": "news",
        "keywords": ["one", "two"],
        "summary": "summary",
        "segmentUrl": "http://islice/segment.ts",
        "coverImgUrl": "http://islice/cover.jpg",
    }


def test_transport_stream_rounding_does_not_create_a_tiny_extra_window() -> None:
    duration = 7200.010333
    total = calculate_total_windows(duration, 3600, 1.0)
    assert total == 2
    assert calculate_window_end(0, total, duration, 3600) == 3600
    assert calculate_window_end(1, total, duration, 3600) == duration


def test_non_boundary_duration_keeps_a_real_final_window() -> None:
    duration = 7201.5
    total = calculate_total_windows(duration, 3600, 1.0)
    assert total == 3
    assert calculate_window_end(2, total, duration, 3600) == duration


def test_short_tail_is_handed_to_next_window() -> None:
    result = process_segments(
        [segment(0, 3400, "accepted"), segment(3400, 3600, "tail")],
        window_start=0,
        chunk_duration=3600,
        is_final_window=False,
        handoff_max_seconds=3000,
        program_start_time=None,
    )
    assert result.segments[0]["accepted"] == 1
    assert result.segments[1]["accepted"] == 0
    assert result.segments[1]["reason"] == "handoff"
    assert result.next_window_start == 3400
    assert result.handoff_start == 3400


def test_exactly_fifty_minutes_is_handed_off() -> None:
    result = process_segments(
        [segment(0, 600), segment(600, 3600)],
        window_start=3600,
        chunk_duration=3600,
        is_final_window=False,
        handoff_max_seconds=3000,
        program_start_time=None,
    )
    assert result.segments[-1]["accepted"] == 0
    assert result.next_window_start == 4200


def test_tail_over_fifty_minutes_is_kept() -> None:
    result = process_segments(
        [segment(0, 3010), segment(3010, 3600)],
        window_start=0,
        chunk_duration=3600,
        is_final_window=False,
        handoff_max_seconds=3000,
        program_start_time=None,
    )
    # The final segment itself is short; the rule applies to that segment, not the first one.
    assert result.segments[-1]["accepted"] == 0

    long_tail = process_segments(
        [segment(0, 500), segment(500, 3600)],
        window_start=0,
        chunk_duration=3600,
        is_final_window=False,
        handoff_max_seconds=3000,
        program_start_time=None,
    )
    assert long_tail.segments[-1]["accepted"] == 1
    assert long_tail.segments[-1]["reason"] == "long_tail_kept"
    assert long_tail.next_window_start == 3600
    assert long_tail.warning


def test_final_window_keeps_tail_and_calculates_absolute_time() -> None:
    base = datetime.fromisoformat("2026-08-03T00:00:00+08:00")
    result = process_segments(
        [segment(0, 50)],
        window_start=7200,
        chunk_duration=50,
        is_final_window=True,
        handoff_max_seconds=3000,
        program_start_time=base,
    )
    assert result.segments[0]["accepted"] == 1
    assert result.segments[0]["absolute_start"] == "2026-08-03T02:00:00+08:00"
    assert result.next_window_start is None


@pytest.mark.parametrize(
    "segments",
    [
        [],
        [segment(20, 10)],
        [segment(0, 100), segment(50, 150)],
        [segment(-5, 10)],
        [segment(0, 200)],
    ],
)
def test_invalid_segments_pause_instead_of_guessing(segments: list[dict]) -> None:
    chunk_duration = 100 if segments and segments[-1].get("endTime") == 200 else 3600
    with pytest.raises(SegmentValidationError):
        process_segments(
            segments,
            window_start=0,
            chunk_duration=chunk_duration,
            is_final_window=False,
            handoff_max_seconds=3000,
            program_start_time=None,
        )
