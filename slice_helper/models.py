from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from pathlib import Path
import re
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
    islice_base_url: str | None = Field(default=None, alias="isliceBaseUrl", max_length=2048)
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

    @field_validator("islice_base_url")
    @classmethod
    def validate_islice_base_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().rstrip("/")
        if not normalized:
            return None
        if not normalized.startswith(("http://", "https://")):
            raise ValueError("isliceBaseUrl must use http:// or https://")
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


class ISliceInstanceUpsert(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    source_id: str = Field(alias="sourceId", min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=255)
    base_url: str = Field(alias="baseUrl", min_length=8, max_length=2048)
    archive_catalog_url: str = Field(
        default="", alias="archiveCatalogUrl", max_length=2048
    )
    schedulable: bool = True
    ssh_host: str = Field(default="", alias="sshHost", max_length=255)
    ssh_port: int = Field(default=22, alias="sshPort", ge=1, le=65535)
    ssh_username: str = Field(default="root", alias="sshUsername", max_length=128)
    ssh_password: str | None = Field(default=None, alias="sshPassword", max_length=1024)
    agent_install_path: str = Field(
        default="", alias="agentInstallPath", max_length=2048
    )
    islice_database_path: str = Field(
        default="/mnt/c/WorkSpace/PublishPackage/iSlice/data/tasks.db",
        alias="isliceDatabasePath",
        max_length=2048,
    )
    storage_root: str = Field(
        default="/mnt/c/WorkSpace/PublishPackage/iSlice/storage",
        alias="storageRoot",
        max_length=2048,
    )
    archive_remote_host: str = Field(
        default="192.168.6.200", alias="archiveRemoteHost", max_length=255
    )
    archive_remote_user: str = Field(
        default="codex", alias="archiveRemoteUser", max_length=128
    )
    archive_remote_root: str = Field(
        default="", alias="archiveRemoteRoot", max_length=2048
    )
    archive_http_base: str = Field(
        default="", alias="archiveHttpBase", max_length=2048
    )
    archive_ssh_key: str = Field(
        default="/root/.ssh/islice_archiver_ed25519",
        alias="archiveSshKey",
        max_length=2048,
    )
    archive_known_hosts: str = Field(
        default="/root/.ssh/known_hosts", alias="archiveKnownHosts", max_length=2048
    )

    @field_validator("source_id")
    @classmethod
    def validate_source_id(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", normalized):
            raise ValueError("sourceId may only contain letters, numbers, dot, underscore and dash")
        return normalized

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        return " ".join(value.split())

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        normalized = value.strip().rstrip("/")
        parsed = urlsplit(normalized)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("baseUrl must be an HTTP URL")
        return normalized

    @field_validator("archive_catalog_url")
    @classmethod
    def validate_catalog_url(cls, value: str) -> str:
        normalized = value.strip().rstrip("/")
        if not normalized:
            return ""
        parsed = urlsplit(normalized)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("archiveCatalogUrl must be an HTTP URL")
        return normalized

    @field_validator(
        "ssh_host", "ssh_username", "archive_remote_host", "archive_remote_user"
    )
    @classmethod
    def normalize_service_text(cls, value: str) -> str:
        normalized = value.strip()
        if any(char in normalized for char in ("\x00", "\r", "\n")):
            raise ValueError("service values cannot contain control characters")
        return normalized

    @field_validator(
        "agent_install_path", "islice_database_path", "storage_root", "archive_remote_root",
        "archive_ssh_key", "archive_known_hosts"
    )
    @classmethod
    def validate_linux_path(cls, value: str) -> str:
        normalized = value.strip().rstrip("/")
        if not normalized:
            return ""
        if not normalized.startswith("/") or any(char in normalized for char in ("\x00", "\r", "\n")):
            raise ValueError("service paths must be absolute Linux paths")
        return normalized

    @field_validator("agent_install_path")
    @classmethod
    def validate_agent_install_path(cls, value: str) -> str:
        if value in {"/", "/bin", "/boot", "/dev", "/etc", "/lib", "/proc", "/sbin", "/sys", "/usr"}:
            raise ValueError("agentInstallPath cannot be a system root directory")
        return value

    @field_validator("archive_http_base")
    @classmethod
    def validate_archive_http_base(cls, value: str) -> str:
        normalized = value.strip().rstrip("/")
        if not normalized:
            return ""
        parsed = urlsplit(normalized)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("archiveHttpBase must be an HTTP URL")
        return normalized

    @model_validator(mode="after")
    def validate_archive_namespace(self):
        source_id = self.source_id
        if self.archive_remote_root:
            remote_source = self.archive_remote_root.rsplit("/", 1)[-1]
            if remote_source != source_id:
                raise ValueError(
                    f"archiveRemoteRoot must end with /{source_id}; "
                    "each iSlice service needs an isolated archive directory"
                )
        if self.archive_http_base:
            http_source = urlsplit(self.archive_http_base).path.rstrip("/").rsplit("/", 1)[-1]
            if http_source != source_id:
                raise ValueError(
                    f"archiveHttpBase must end with /{source_id}; "
                    "each iSlice service needs an isolated HTTP namespace"
                )
        if self.archive_catalog_url:
            catalog_path = urlsplit(self.archive_catalog_url).path.rstrip("/")
            expected_suffix = f"/{source_id}/catalog.json"
            if not catalog_path.endswith(expected_suffix):
                raise ValueError(
                    f"archiveCatalogUrl must end with {expected_suffix}"
                )
        return self


class ISliceMigrationRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    base_url: str = Field(alias="baseUrl", min_length=8, max_length=2048)
    ssh_host: str = Field(alias="sshHost", min_length=1, max_length=255)
    ssh_port: int = Field(default=22, alias="sshPort", ge=1, le=65535)
    ssh_username: str = Field(alias="sshUsername", min_length=1, max_length=128)
    ssh_password: str | None = Field(default=None, alias="sshPassword", max_length=1024)
    agent_install_path: str = Field(alias="agentInstallPath", min_length=1, max_length=2048)
    islice_database_path: str = Field(alias="isliceDatabasePath", min_length=1, max_length=2048)
    storage_root: str = Field(alias="storageRoot", min_length=1, max_length=2048)

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        normalized = value.strip().rstrip("/")
        parsed = urlsplit(normalized)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("baseUrl must be an HTTP URL")
        return normalized

    @field_validator("ssh_host", "ssh_username")
    @classmethod
    def normalize_migration_text(cls, value: str) -> str:
        value = value.strip()
        if any(char in value for char in ("\x00", "\r", "\n")):
            raise ValueError("migration values cannot contain control characters")
        return value

    @field_validator("agent_install_path", "islice_database_path", "storage_root")
    @classmethod
    def validate_migration_path(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        if not value.startswith("/"):
            raise ValueError("migration paths must be absolute Linux paths")
        return value


class SchedulingPriorityUpdate(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    priority: str = Field(alias="priority", pattern=r"^(fewest_completed|most_completed)$")


class SystemResetExecute(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    request_id: str = Field(alias="requestId", pattern=r"^[0-9a-f]{32}$")
    confirmation_text: str = Field(alias="confirmationText", min_length=8, max_length=100)
    receipts: list[dict[str, object]] = Field(min_length=1, max_length=100)
    acknowledge_media_handling: bool = Field(alias="acknowledgeMediaHandling")


class TimeReferenceUpdate(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    program_start_time: datetime = Field(alias="programStartTime")


class TimeReferenceRefresh(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    program_start_time: datetime | None = Field(default=None, alias="programStartTime")


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


class SegmentMergePreviewRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    segment_ids: list[int] = Field(alias="segmentIds", min_length=2)
    primary_segment_id: int = Field(alias="primarySegmentId", ge=1)

    @field_validator("segment_ids")
    @classmethod
    def unique_segment_ids(cls, value: list[int]) -> list[int]:
        if any(item < 1 for item in value):
            raise ValueError("segmentIds must contain positive IDs")
        if len(set(value)) != len(value):
            raise ValueError("segmentIds must not contain duplicates")
        return value


class SegmentMergeCreate(SegmentMergePreviewRequest):
    preview_token: str = Field(alias="previewToken", min_length=64, max_length=64)


class SegmentMergeUpdate(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    title: str | None = Field(default=None, max_length=500)
    content_type: str | None = Field(default=None, alias="contentType", max_length=100)
    ignored: bool | None = None

    @field_validator("title", "content_type")
    @classmethod
    def normalize_merge_text(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None

    @model_validator(mode="after")
    def require_merge_update(self):
        if not self.model_fields_set.intersection({"title", "content_type", "ignored"}):
            raise ValueError("At least one merge field must be supplied")
        if (
            "content_type" in self.model_fields_set
            and self.content_type not in CONTENT_TYPES
        ):
            raise ValueError("节目类型必须从预设选项中选择")
        return self


class WindowResplitRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    task_id: str = Field(
        alias="taskId", min_length=1, max_length=64, pattern=r"^[\x21-\x7e]+$"
    )


class TailRebuildRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    preview_token: str = Field(alias="previewToken", min_length=64, max_length=64)
    confirmation_text: str = Field(
        alias="confirmationText", min_length=1, max_length=100
    )


class MediaProbe(BaseModel):
    duration: float
    format_name: str
    video_codec: str
    audio_codec: str | None = None


class HealthResult(BaseModel):
    status: str
    checks: dict[str, str] = Field(default_factory=dict)
