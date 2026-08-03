from __future__ import annotations

import asyncio
from typing import Any

import httpx

from .config import Settings


class ISliceError(RuntimeError):
    pass


class ISliceConflictError(ISliceError):
    pass


class ISliceClient:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self.client = httpx.AsyncClient(
            base_url=settings.islice_base_url,
            timeout=httpx.Timeout(30.0, connect=10.0),
            follow_redirects=True,
            trust_env=False,
            transport=transport,
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def get_task_info(self, task_id: str) -> dict[str, Any] | None:
        try:
            response = await self.client.post("/GetTaskInfo", json={"taskId": task_id})
        except httpx.HTTPError as exc:
            raise ISliceError(f"GetTaskInfo transport error: {exc}") from exc
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise ISliceError(f"GetTaskInfo returned HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ISliceError("GetTaskInfo returned invalid JSON") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("taskInfo"), dict):
            raise ISliceError("GetTaskInfo response is missing taskInfo")
        return payload

    async def ensure_task(self, task_id: str, request: dict[str, Any]) -> dict[str, Any] | None:
        existing = await self.get_task_info(task_id)
        if existing is not None:
            self._verify_video_path(existing, request["videoPath"])
            return existing
        try:
            response = await self.client.post("/CreateTask", json=request)
        except httpx.HTTPError as exc:
            # The create may have reached iSlice. Re-query the same deterministic ID before
            # allowing the orchestrator to consume a new attempt number.
            for delay in (1.0, 3.0):
                await asyncio.sleep(delay)
                try:
                    existing = await self.get_task_info(task_id)
                except ISliceError:
                    continue
                if existing is not None:
                    self._verify_video_path(existing, request["videoPath"])
                    return existing
            raise ISliceError(f"CreateTask transport error: {exc}") from exc
        if response.status_code == 200:
            return None
        if response.status_code == 409:
            existing = await self.get_task_info(task_id)
            if existing is None:
                raise ISliceError("CreateTask returned 409 but GetTaskInfo returned 404")
            self._verify_video_path(existing, request["videoPath"])
            return existing
        raise ISliceError(f"CreateTask returned HTTP {response.status_code}")

    @staticmethod
    def _verify_video_path(payload: dict[str, Any], expected: str) -> None:
        actual = str(payload.get("taskInfo", {}).get("videoPath") or "")
        if actual != expected:
            raise ISliceConflictError(
                f"Existing task videoPath does not match: expected {expected}, got {actual}"
            )

    async def ping(self) -> tuple[bool, str]:
        try:
            response = await self.client.get("/openapi.json", timeout=5.0)
            if response.status_code < 500:
                return True, f"HTTP {response.status_code}"
            return False, f"HTTP {response.status_code}"
        except httpx.HTTPError as exc:
            return False, str(exc)
