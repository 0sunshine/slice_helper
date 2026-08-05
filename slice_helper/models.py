from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator


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
    template_id: str = Field(default="general", alias="templateId")
    language: str = "zh"
    channel_name: str | None = Field(default=None, alias="channelName", max_length=255)
    program_start_time: datetime | None = Field(default=None, alias="programStartTime")
    cut_mode: CutMode = Field(default=CutMode.COPY, alias="cutMode")

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


class MediaProbe(BaseModel):
    duration: float
    format_name: str
    video_codec: str
    audio_codec: str | None = None


class HealthResult(BaseModel):
    status: str
    checks: dict[str, str] = Field(default_factory=dict)
