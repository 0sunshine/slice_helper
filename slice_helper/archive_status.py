from __future__ import annotations

from typing import Any

import httpx


class ArchiveCatalogReader:
    def __init__(self) -> None:
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(15.0, connect=5.0),
            follow_redirects=True,
            trust_env=False,
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def read(
        self,
        instances: list[dict[str, Any]],
        contexts: dict[tuple[str, str], dict[str, Any]],
    ) -> dict[str, Any]:
        sources: list[dict[str, Any]] = []
        tasks: list[dict[str, Any]] = []
        for instance in instances:
            source_id = str(instance["source_id"])
            catalog_url = str(instance.get("archive_catalog_url") or "")
            source = {
                "id": source_id,
                "name": instance["name"],
                "baseUrl": instance["base_url"],
                "catalogUrl": catalog_url,
                "schedulable": bool(instance["schedulable"]),
                "online": False,
                "generatedAt": None,
                "summary": {"taskCount": 0, "totalBytes": 0, "states": {}},
                "error": "",
            }
            if not catalog_url:
                source["error"] = "未配置归档 catalog 地址"
                sources.append(source)
                continue
            try:
                response = await self.client.get(catalog_url)
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict) or not isinstance(payload.get("tasks"), list):
                    raise ValueError("catalog 格式无效")
                payload_source = payload.get("source")
                catalog_source_id = (
                    str(payload_source.get("id") or "")
                    if isinstance(payload_source, dict)
                    else ""
                )
                if catalog_source_id and catalog_source_id != source_id:
                    raise ValueError(
                        f"catalog sourceId 不匹配：{catalog_source_id} != {source_id}"
                    )
            except (httpx.HTTPError, ValueError) as exc:
                source["error"] = str(exc)
                sources.append(source)
                continue
            source["online"] = True
            source["generatedAt"] = payload.get("generatedAt")
            source["summary"] = payload.get("summary") or source["summary"]
            sources.append(source)
            for stored in payload["tasks"]:
                if not isinstance(stored, dict):
                    continue
                task = dict(stored)
                task["source_id"] = source_id
                task["source_name"] = instance["name"]
                context = contexts.get((source_id, str(task.get("task_id") or "")))
                if context:
                    task["context"] = context
                tasks.append(task)
        return {"sources": sources, "tasks": tasks}
