from __future__ import annotations

import os
from pathlib import Path

import httpx


class SourceDownloadError(RuntimeError):
    pass


class HttpSourceDownloader:
    CHUNK_SIZE = 1024 * 1024

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport

    async def download(self, url: str, target: Path) -> int:
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(f"{target.stem}.partial{target.suffix}")
        partial.unlink(missing_ok=True)
        written = 0
        completed = False
        timeout = httpx.Timeout(connect=15.0, read=300.0, write=30.0, pool=30.0)

        try:
            async with httpx.AsyncClient(
                follow_redirects=True,
                timeout=timeout,
                trust_env=False,
                transport=self._transport,
                headers={"Accept-Encoding": "identity"},
            ) as client:
                async with client.stream("GET", url) as response:
                    try:
                        response.raise_for_status()
                    except httpx.HTTPStatusError as exc:
                        raise SourceDownloadError(
                            f"Source download returned HTTP {response.status_code}"
                        ) from exc

                    expected = self._content_length(response)
                    with partial.open("wb") as output:
                        async for chunk in response.aiter_bytes(self.CHUNK_SIZE):
                            if not chunk:
                                continue
                            output.write(chunk)
                            written += len(chunk)
                        output.flush()
                        os.fsync(output.fileno())

            if written <= 0:
                raise SourceDownloadError("Source download returned an empty body")
            if expected is not None and written != expected:
                raise SourceDownloadError(
                    f"Source download length mismatch: expected {expected}, got {written}"
                )
            os.replace(partial, target)
            completed = True
            return written
        except SourceDownloadError:
            raise
        except (httpx.HTTPError, OSError) as exc:
            raise SourceDownloadError(f"Source download failed: {exc}") from exc
        finally:
            if not completed:
                partial.unlink(missing_ok=True)

    @staticmethod
    def _content_length(response: httpx.Response) -> int | None:
        raw = response.headers.get("content-length")
        if raw is None:
            return None
        try:
            value = int(raw)
        except ValueError as exc:
            raise SourceDownloadError("Source download returned invalid Content-Length") from exc
        if value < 0:
            raise SourceDownloadError("Source download returned invalid Content-Length")
        return value
