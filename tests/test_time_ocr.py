from __future__ import annotations

from datetime import datetime

import pytest

from slice_helper.time_ocr import OcrText, TimeOcrError, parse_time_reference


def test_parses_broadcast_timestamp_and_applies_frame_offset() -> None:
    result = parse_time_reference(
        [
            OcrText("丽江新闻综合频道（主频道）", 0.98),
            OcrText("2026-06-20 12:29:59", 0.99),
        ],
        frame_offset_seconds=2.0,
    )

    assert result.observed_time == datetime(2026, 6, 20, 12, 29, 59)
    assert result.source_start_time == datetime(2026, 6, 20, 12, 29, 57)
    assert result.matched_text == "2026-06-20 12:29:59"
    assert result.confidence == 0.99


def test_parses_date_and_time_from_adjacent_ocr_lines() -> None:
    result = parse_time_reference(
        [OcrText("２０２６／０６／２０", 0.97), OcrText("１２：３０：００", 0.96)]
    )

    assert result.source_start_time == datetime(2026, 6, 20, 12, 30, 0)
    assert result.confidence == 0.96


def test_rejects_ocr_without_a_complete_timestamp() -> None:
    with pytest.raises(TimeOcrError, match="No complete timestamp"):
        parse_time_reference([OcrText("新闻综合", 0.99), OcrText("12:30", 0.98)])
