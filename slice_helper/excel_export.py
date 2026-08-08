from __future__ import annotations

import io
import json
import re
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import PurePosixPath, PureWindowsPath
from urllib.parse import unquote, urlsplit

import xlsxwriter


CHINA_TIMEZONE = timezone(timedelta(hours=8))


HEADERS = (
    "序号",
    "频道",
    "业务日期",
    "真实开始",
    "真实结束",
    "时长",
    "标题",
    "节目类型",
    "新闻事件类型",
    "主题",
    "关键词",
    "摘要",
    "视频链接",
    "封面链接",
    "来源文件",
    "作业 ID",
    "窗口号",
    "iSlice 任务 ID",
    "窗口内开始（秒）",
    "窗口内结束（秒）",
    "源全局开始（秒）",
    "源全局结束（秒）",
)


def safe_export_filename(channel_name: str) -> str:
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", channel_name).strip(" ._")
    return f"{safe or '频道'}-拆条结果.xlsx"


def build_channel_workbook(export: dict) -> bytes:
    channel = export["channel"]
    segments_by_date: dict[str, list[dict]] = defaultdict(list)
    for segment in export["segments"]:
        segments_by_date[str(segment["broadcast_date"])].append(segment)
    job_dates = [str(job["broadcast_date"]) for job in export["jobs"]]
    sheet_dates = sorted(dict.fromkeys(job_dates))

    output = io.BytesIO()
    workbook = xlsxwriter.Workbook(
        output,
        {
            "in_memory": True,
            "strings_to_formulas": False,
            "strings_to_urls": False,
        },
    )
    workbook.set_properties(
        {
            "title": f"{channel['name']} 拆条结果",
            "subject": "按业务日期汇总的最终采用拆条结果",
            "company": "iSlice",
        }
    )
    formats = _formats(workbook)

    if not sheet_dates:
        sheet = workbook.add_worksheet("暂无数据")
        sheet.hide_gridlines(2)
        sheet.write(0, 0, "该频道暂无可导出的作业", formats["empty"])
        sheet.set_column(0, 0, 34)
    for broadcast_date in sheet_dates:
        sheet = workbook.add_worksheet(broadcast_date)
        _write_date_sheet(
            sheet,
            channel_name=str(channel["name"]),
            broadcast_date=broadcast_date,
            segments=segments_by_date.get(broadcast_date, []),
            formats=formats,
        )

    workbook.close()
    return output.getvalue()


def _formats(workbook: xlsxwriter.Workbook) -> dict[str, xlsxwriter.format.Format]:
    return {
        "header": workbook.add_format(
            {
                "bold": True,
                "font_color": "#FFFFFF",
                "bg_color": "#176B57",
                "align": "center",
                "valign": "vcenter",
                "border": 0,
            }
        ),
        "text": workbook.add_format({"valign": "top"}),
        "wrap": workbook.add_format({"valign": "top", "text_wrap": True}),
        "integer": workbook.add_format({"num_format": "0", "valign": "top"}),
        "seconds": workbook.add_format({"num_format": "0.00", "valign": "top"}),
        "date": workbook.add_format({"num_format": "yyyy-mm-dd", "valign": "top"}),
        "datetime": workbook.add_format(
            {"num_format": "yyyy-mm-dd hh:mm:ss", "valign": "top"}
        ),
        "duration": workbook.add_format(
            {"num_format": "[h]:mm:ss.00", "valign": "top"}
        ),
        "link": workbook.add_format(
            {"font_color": "#176B57", "underline": True, "valign": "top"}
        ),
        "empty": workbook.add_format(
            {"font_color": "#626A64", "italic": True, "valign": "top"}
        ),
    }


def _write_date_sheet(
    sheet,
    *,
    channel_name: str,
    broadcast_date: str,
    segments: list[dict],
    formats: dict,
) -> None:
    sheet.hide_gridlines(2)
    sheet.freeze_panes(1, 3)
    sheet.set_row(0, 28)
    for column, header in enumerate(HEADERS):
        sheet.write(0, column, header, formats["header"])

    business_date = date.fromisoformat(broadcast_date)
    for index, segment in enumerate(segments, start=1):
        row = index
        absolute_start = _excel_datetime(segment.get("absolute_start"))
        absolute_end = _excel_datetime(segment.get("absolute_end"))
        local_start = float(segment.get("local_start") or 0)
        local_end = float(segment.get("local_end") or 0)
        global_start = float(segment.get("global_start") or 0)
        global_end = float(segment.get("global_end") or 0)
        duration_days = max(0.0, global_end - global_start) / 86400
        source = str(segment.get("source_url") or segment.get("source_path") or "")
        keywords = _keywords(segment.get("keywords_json"))

        sheet.write_number(row, 0, index, formats["integer"])
        sheet.write(row, 1, channel_name, formats["text"])
        sheet.write_datetime(row, 2, business_date, formats["date"])
        _write_datetime(sheet, row, 3, absolute_start, formats)
        _write_datetime(sheet, row, 4, absolute_end, formats)
        excel_row = row + 1
        sheet.write_formula(
            row,
            5,
            f'=IF(AND(D{excel_row}<>"",E{excel_row}<>""),E{excel_row}-D{excel_row},(V{excel_row}-U{excel_row})/86400)',
            formats["duration"],
            duration_days,
        )
        sheet.write(row, 6, str(segment.get("title") or ""), formats["wrap"])
        sheet.write(row, 7, str(segment.get("content_type") or ""), formats["text"])
        sheet.write(row, 8, str(segment.get("news_event_type") or ""), formats["text"])
        sheet.write(row, 9, str(segment.get("topic") or ""), formats["text"])
        sheet.write(row, 10, ", ".join(keywords), formats["wrap"])
        sheet.write(row, 11, str(segment.get("summary") or ""), formats["wrap"])
        _write_url(sheet, row, 12, str(segment.get("segment_url") or ""), formats)
        _write_url(sheet, row, 13, str(segment.get("cover_img_url") or ""), formats)
        sheet.write(row, 14, _source_name(source), formats["text"])
        sheet.write(row, 15, str(segment.get("job_id") or ""), formats["text"])
        sheet.write_number(
            row, 16, int(segment.get("window_index") or 0) + 1, formats["integer"]
        )
        sheet.write(row, 17, str(segment.get("task_id") or ""), formats["text"])
        sheet.write_number(row, 18, local_start, formats["seconds"])
        sheet.write_number(row, 19, local_end, formats["seconds"])
        sheet.write_number(row, 20, global_start, formats["seconds"])
        sheet.write_number(row, 21, global_end, formats["seconds"])

    last_row = max(1, len(segments))
    sheet.autofilter(0, 0, last_row, len(HEADERS) - 1)
    sheet.set_column("A:A", 7)
    sheet.set_column("B:B", 20)
    sheet.set_column("C:C", 12)
    sheet.set_column("D:F", 21)
    sheet.set_column("G:G", 34)
    sheet.set_column("H:J", 16)
    sheet.set_column("K:K", 30)
    sheet.set_column("L:L", 52)
    sheet.set_column("M:N", 36)
    sheet.set_column("O:O", 34)
    sheet.set_column("P:P", 34)
    sheet.set_column("Q:Q", 9)
    sheet.set_column("R:R", 32)
    sheet.set_column("S:V", 18)


def _excel_datetime(value: object) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(CHINA_TIMEZONE).replace(tzinfo=None)
    return parsed


def _write_datetime(sheet, row: int, column: int, value: datetime | None, formats) -> None:
    if value is None:
        sheet.write_blank(row, column, None, formats["datetime"])
    else:
        sheet.write_datetime(row, column, value, formats["datetime"])


def _write_url(sheet, row: int, column: int, value: str, formats) -> None:
    if value:
        sheet.write_url(row, column, value, formats["link"], value)
    else:
        sheet.write_blank(row, column, None, formats["text"])


def _keywords(value: object) -> list[str]:
    try:
        parsed = json.loads(str(value or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return [str(item) for item in parsed] if isinstance(parsed, list) else []


def _source_name(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme in {"http", "https"}:
        return unquote(PurePosixPath(parsed.path).name) or parsed.netloc
    return PureWindowsPath(value).name or PurePosixPath(value).name
