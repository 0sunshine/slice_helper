from __future__ import annotations

import asyncio
from typing import Any

import httpx

from .config import Settings


class ISliceError(RuntimeError):
    pass


class ISliceConflictError(ISliceError):
    pass


class ISliceConfigurationError(ISliceError):
    pass


class ISliceClient:
    def __init__(
        self,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None = None,
        base_url: str | None = None,
    ):
        self.settings = settings
        self.base_url = (base_url or settings.islice_base_url).rstrip("/")
        self.client = httpx.AsyncClient(
            base_url=self.base_url,
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

    async def delete_task(self, task_id: str) -> bool:
        """Delete a task, treating an already absent task as an idempotent success."""
        try:
            response = await self.client.post("/DeleteTask", json={"taskId": task_id})
        except httpx.HTTPError as exc:
            raise ISliceError(f"DeleteTask transport error: {exc}") from exc
        if response.status_code == 200:
            return True
        if response.status_code == 404:
            return False
        raise ISliceError(f"DeleteTask returned HTTP {response.status_code}")

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


class ISlicePool:
    def __init__(self, settings: Settings, urls: tuple[str, ...] | None = None):
        self.settings = settings
        self.clients = {
            url: ISliceClient(settings, base_url=url)
            for url in (urls or settings.configured_islice_urls)
        }
        self._lock = asyncio.Lock()

    @property
    def urls(self) -> tuple[str, ...]:
        return tuple(self.clients)

    def get_client(self, base_url: str) -> ISliceClient:
        normalized = base_url.rstrip("/")
        try:
            return self.clients[normalized]
        except KeyError as exc:
            raise ISliceConfigurationError(
                f"Job is assigned to unconfigured iSlice instance: {normalized}"
            ) from exc

    async def reconcile(self, urls: tuple[str, ...]) -> None:
        normalized = tuple(dict.fromkeys(url.rstrip("/") for url in urls if url))
        async with self._lock:
            for url in normalized:
                if url not in self.clients:
                    self.clients[url] = ISliceClient(self.settings, base_url=url)
            removed = [
                self.clients.pop(url)
                for url in tuple(self.clients)
                if url not in normalized
            ]
        if removed:
            await asyncio.gather(*(client.close() for client in removed))

    async def close(self) -> None:
        await asyncio.gather(*(client.close() for client in self.clients.values()))

    async def ping(self) -> tuple[bool, dict[str, str]]:
        results = await asyncio.gather(*(client.ping() for client in self.clients.values()))
        checks = {
            url: message
            for url, (_ok, message) in zip(self.clients, results, strict=True)
        }
        return all(ok for ok, _message in results), checks
