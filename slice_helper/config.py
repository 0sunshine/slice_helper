from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    raw = os.getenv(name, str(default)).strip()
    value = int(raw)
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return value


def _env_float(name: str, default: float, minimum: float = 0.1) -> float:
    raw = os.getenv(name, str(default)).strip()
    value = float(raw)
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return value


@dataclass(frozen=True, slots=True)
class Settings:
    islice_base_url: str
    public_base_url: str
    host: str
    port: int
    data_dir: Path
    temp_dir: Path
    ffmpeg_path: str
    ffprobe_path: str
    max_active_jobs: int
    poll_interval_seconds: float
    window_timeout_seconds: float
    ffmpeg_timeout_seconds: float
    window_seconds: float = 3600.0
    window_boundary_tolerance_seconds: float = 1.0
    handoff_max_seconds: float = 3000.0
    # One initial submission plus three retries using the 5/15/45s backoff.
    max_service_attempts: int = 4

    @property
    def database_path(self) -> Path:
        return self.data_dir / "slice_helper.db"

    @classmethod
    def from_env(cls, base_dir: Path | None = None) -> "Settings":
        root = (base_dir or Path.cwd()).resolve()

        def resolve_dir(name: str, default: str) -> Path:
            value = Path(os.getenv(name, default)).expanduser()
            if not value.is_absolute():
                value = root / value
            return value.resolve()

        islice_url = os.getenv("ISLICE_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
        public_url = os.getenv("PUBLIC_BASE_URL", "http://127.0.0.1:8090").rstrip("/")
        if not islice_url.startswith(("http://", "https://")):
            raise ValueError("ISLICE_BASE_URL must use http:// or https://")
        if not public_url.startswith(("http://", "https://")):
            raise ValueError("PUBLIC_BASE_URL must use http:// or https://")

        return cls(
            islice_base_url=islice_url,
            public_base_url=public_url,
            host=os.getenv("HOST", "0.0.0.0"),
            port=_env_int("PORT", 8090),
            data_dir=resolve_dir("DATA_DIR", "data"),
            temp_dir=resolve_dir("TEMP_DIR", "temp"),
            ffmpeg_path=os.getenv("FFMPEG_PATH", "ffmpeg"),
            ffprobe_path=os.getenv("FFPROBE_PATH", "ffprobe"),
            max_active_jobs=_env_int("MAX_ACTIVE_JOBS", 1),
            poll_interval_seconds=_env_float("POLL_INTERVAL_SECONDS", 15.0),
            window_timeout_seconds=_env_float("WINDOW_TIMEOUT_SECONDS", 21600.0),
            ffmpeg_timeout_seconds=_env_float("FFMPEG_TIMEOUT_SECONDS", 7200.0),
        )
