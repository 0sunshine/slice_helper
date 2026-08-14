from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import quote, unquote, urljoin, urlsplit

import httpx


TASK_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")
MAX_SEGMENTS_JSON_BYTES = 10 * 1024 * 1024


class ArchivePreviewError(RuntimeError):
    pass


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
                if response.status_code == 404:
                    raise ValueError("归档代理尚未发布 catalog，请先部署/拉起代理")
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

    @staticmethod
    def _catalog_root(catalog_url: str) -> str:
        parts = urlsplit(catalog_url)
        path = parts.path.rsplit("/", 1)[0].rstrip("/")
        return f"{parts.scheme}://{parts.netloc}{path}"

    @staticmethod
    def _validate_archive_url(url: str, catalog_root: str) -> str:
        candidate = urljoin(catalog_root.rstrip("/") + "/", url)
        root_parts = urlsplit(catalog_root)
        candidate_parts = urlsplit(candidate)
        root_path = root_parts.path.rstrip("/")
        if (
            candidate_parts.scheme != root_parts.scheme
            or candidate_parts.netloc != root_parts.netloc
            or not (
                candidate_parts.path == root_path
                or candidate_parts.path.startswith(root_path + "/")
            )
        ):
            raise ArchivePreviewError("归档预览地址超出已配置的 catalog 目录")
        return candidate.rstrip("/")

    @staticmethod
    def _media_filename(raw_url: Any, directory: str) -> str | None:
        value = str(raw_url or "")
        if not value:
            return None
        parts = [unquote(part) for part in urlsplit(value).path.strip("/").split("/")]
        if len(parts) < 2 or parts[-2] != directory:
            return None
        filename = parts[-1]
        if not filename or filename in {".", ".."} or any(
            marker in filename for marker in ("/", "\\", "\x00")
        ):
            return None
        return filename

    async def read_task_preview(
        self,
        instance: dict[str, Any],
        task_id: str,
        revision_digest: str | None = None,
    ) -> dict[str, Any]:
        if not TASK_ID_PATTERN.fullmatch(task_id):
            raise ArchivePreviewError("task ID 格式无效")
        if revision_digest and not DIGEST_PATTERN.fullmatch(revision_digest):
            raise ArchivePreviewError("归档版本 Digest 格式无效")
        catalog_url = str(instance.get("archive_catalog_url") or "")
        if not catalog_url:
            raise ArchivePreviewError("该 iSlice 实例未配置归档 catalog 地址")
        try:
            response = await self.client.get(catalog_url)
            response.raise_for_status()
            catalog = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ArchivePreviewError(f"无法读取归档 catalog：{exc}") from exc
        if not isinstance(catalog, dict) or not isinstance(catalog.get("tasks"), list):
            raise ArchivePreviewError("归档 catalog 格式无效")
        catalog_source = catalog.get("source")
        catalog_source_id = (
            str(catalog_source.get("id") or "")
            if isinstance(catalog_source, dict)
            else ""
        )
        source_id = str(instance.get("source_id") or "")
        if catalog_source_id and catalog_source_id != source_id:
            raise ArchivePreviewError("归档 catalog 的 sourceId 与实例配置不匹配")
        task = next(
            (
                item
                for item in catalog["tasks"]
                if isinstance(item, dict) and str(item.get("task_id") or "") == task_id
            ),
            None,
        )
        # A catalog can be regenerated on a replacement node and temporarily
        # omit tasks that were archived by the previous node.  The archive
        # namespace is immutable, so probe the deterministic task directory
        # before declaring the old completed window unavailable.
        direct_archive_url = ""
        if task is None:
            catalog_root = self._catalog_root(catalog_url)
            direct_archive_url = f"{catalog_root}/tasks/{quote(task_id, safe='')}"
            task = {}

        selected_digest = revision_digest or str(task.get("manifest_digest") or "")
        archive_url = str(task.get("archive_url") or "")
        if revision_digest:
            revision = next(
                (
                    item
                    for item in task.get("revisions") or []
                    if isinstance(item, dict)
                    and str(item.get("manifest_digest") or "") == revision_digest
                ),
                None,
            )
            if revision is None:
                raise ArchivePreviewError("归档 catalog 中不存在指定版本")
            archive_url = str(revision.get("archive_url") or "")
            if not archive_url and revision_digest != str(task.get("published_digest") or ""):
                raise ArchivePreviewError("旧版 catalog 没有该历史版本的预览地址，请先升级并运行归档代理")

        catalog_root = self._catalog_root(catalog_url)
        if not archive_url and direct_archive_url:
            archive_url = direct_archive_url
        if not archive_url:
            archive_url = (
                f"{catalog_root}/download/{quote(task_id, safe='')}"
            )
        archive_url = self._validate_archive_url(archive_url, catalog_root)
        segments_json_url = f"{archive_url}/segments.json"
        try:
            async with self.client.stream("GET", segments_json_url) as segments_response:
                segments_response.raise_for_status()
                final_url = str(segments_response.url)
                self._validate_archive_url(final_url, catalog_root)
                declared_size = int(
                    segments_response.headers.get("Content-Length", "0") or 0
                )
                if declared_size > MAX_SEGMENTS_JSON_BYTES:
                    raise ArchivePreviewError("归档 segments.json 超过 10 MB 限制")
                content = bytearray()
                async for chunk in segments_response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > MAX_SEGMENTS_JSON_BYTES:
                        raise ArchivePreviewError("归档 segments.json 超过 10 MB 限制")
            payload = json.loads(content)
        except ArchivePreviewError:
            raise
        except (httpx.HTTPError, ValueError) as exc:
            raise ArchivePreviewError(f"无法读取归档 segments.json：{exc}") from exc
        raw_segments = payload.get("segments") if isinstance(payload, dict) else None
        if not isinstance(raw_segments, list):
            raise ArchivePreviewError("归档 segments.json 格式无效")

        segments: list[dict[str, Any]] = []
        warnings: list[str] = []
        for index, raw in enumerate(raw_segments):
            if not isinstance(raw, dict):
                warnings.append(f"第 {index + 1} 条片段不是对象")
                continue
            segment = dict(raw)
            video_name = self._media_filename(raw.get("segmentUrl"), "segments")
            cover_name = self._media_filename(raw.get("coverImgUrl"), "covers")
            segment["segmentUrl"] = (
                f"{archive_url}/segments/{quote(video_name, safe='')}"
                if video_name
                else ""
            )
            segment["coverImgUrl"] = (
                f"{archive_url}/covers/{quote(cover_name, safe='')}"
                if cover_name
                else ""
            )
            segment["sourceIndex"] = index
            if not video_name:
                warnings.append(f"第 {index + 1} 条片段没有可映射的归档视频")
            segments.append(segment)
        return {
            "sourceId": source_id,
            "sourceName": instance.get("name") or source_id,
            "taskId": task_id,
            "revisionDigest": selected_digest,
            "archiveUrl": archive_url,
            "segmentsJsonUrl": segments_json_url,
            "segments": segments,
            "warnings": warnings,
            "externalMedia": True,
        }
