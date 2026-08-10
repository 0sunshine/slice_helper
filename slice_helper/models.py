from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


CONTENT_TYPES = (
    "新闻",
    "电视剧",
    "电影",
    "综艺",
    "少儿",
    "体育",
    "纪录片",
    "科教",
    "文艺",
    "生活服务",
    "商业广告",
    "公益广告",
    "电视购物",
    "其他",
)


class JobStatus(StrEnum):
    PENDING_SCHEDULE = "pending_schedule"
    QUEUED = "queued"
    PROBING = "probing"
    RUNNING = "running"
    PAUSE_REQUESTED = "pause_requested"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    STOP_REQUESTED = "stop_requested"
    STOPPED = "stopped"


class WindowStatus(StrEnum):
    PENDING = "pending"
    CUTTING = "cutting"
    READY = "ready"
    SUBMITTED = "submitted"
    POLLING = "polling"
    COMPLETED = "completed"
    FAILED = "failed"


class CutMode(StrEnum):
    COPY = "copy"
    TRANSCODE = "transcode"


class JobCreate(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    source_path: str = Field(alias="sourcePath", min_length=1, max_length=4096)
    channel_id: str = Field(alias="channelId", min_length=32, max_length=32)
    broadcast_date: date = Field(alias="broadcastDate")
    template_id: str = Field(default="general", alias="templateId")
    language: str = "zh"
    program_start_time: datetime | None = Field(default=None, alias="programStartTime")
    cut_mode: CutMode = Field(default=CutMode.COPY, alias="cutMode")
    overwrite: bool = False

    @field_validator("source_path")
    @classmethod
    def validate_source_path(cls, value: str) -> str:
        normalized = value.strip()
        parsed = urlsplit(normalized)
        if parsed.scheme.lower() in {"http", "https"}:
            if not parsed.netloc:
                raise ValueError("sourcePath HTTP URL must include a host")
            return normalized
        path = Path(normalized).expanduser()
        if not path.is_absolute():
            raise ValueError("sourcePath must be an absolute server path or HTTP URL")
        if path.suffix.lower() != ".ts":
            raise ValueError("sourcePath must point to a .ts file")
        return str(path)

    @field_validator("template_id")
    @classmethod
    def validate_template(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"general", "sports", "live"}:
            raise ValueError("templateId must be general, sports, or live")
        return normalized

    @field_validator("language")
    @classmethod
    def validate_language(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"zh", "en"}:
            raise ValueError("language must be zh or en")
        return normalized


class ChannelCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("Channel name is required")
        return normalized


class ChannelUpdate(ChannelCreate):
    pass


class TimeReferenceUpdate(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    program_start_time: datetime = Field(alias="programStartTime")


class JobReviewUpdate(BaseModel):
    reviewed: bool


class SegmentUpdate(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    title: str | None = Field(default=None, max_length=500)
    content_type: str | None = Field(default=None, alias="contentType", max_length=100)
    ignored: bool | None = None
    restored_from_task: bool = Field(default=False, alias="restoredFromTask")

    @field_validator("title", "content_type")
    @classmethod
    def normalize_text(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None

    @model_validator(mode="after")
    def require_update(self):
        editable_fields = {"title", "content_type", "ignored"}
        if not self.model_fields_set.intersection(editable_fields):
            raise ValueError("At least one segment field must be supplied")
        if (
            "content_type" in self.model_fields_set
            and self.content_type not in CONTENT_TYPES
            and not self.restored_from_task
        ):
            raise ValueError("节目类型必须从预设选项中选择")
        return self


class WindowResplitRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    task_id: str = Field(
        alias="taskId", min_length=1, max_length=64, pattern=r"^[\x21-\x7e]+$"
    )


class WindowRangePreviewRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    start_window_index: int = Field(alias="startWindowIndex", ge=0)
    end_window_index: int = Field(alias="endWindowIndex", ge=0)

    @model_validator(mode="after")
    def validate_range(self):
        if self.end_window_index < self.start_window_index:
            raise ValueError("endWindowIndex must be greater than or equal to startWindowIndex")
        return self


class WindowRangeResplitRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    preview_id: str = Field(alias="previewId", min_length=16, max_length=64)
    confirmation_text: str = Field(alias="confirmationText", min_length=1, max_length=64)


class MediaProbe(BaseModel):
    duration: float
    format_name: str
    video_codec: str
    audio_codec: str | None = None


class HealthResult(BaseModel):
    status: str
    checks: dict[str, str] = Field(default_factory=dict)
