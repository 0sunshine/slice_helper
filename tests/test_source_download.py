from pathlib import Path

import httpx
import pytest

from slice_helper.source_download import HttpSourceDownloader, SourceDownloadError


@pytest.mark.asyncio
async def test_http_source_download_is_atomic(tmp_path: Path) -> None:
    body = b"ts-data" * 1024

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["accept-encoding"] == "identity"
        return httpx.Response(
            200,
            headers={"Content-Length": str(len(body))},
            content=body,
        )

    target = tmp_path / "source.ts"
    downloader = HttpSourceDownloader(httpx.MockTransport(handler))
    written = await downloader.download("http://media.test/source.ts", target)

    assert written == len(body)
    assert target.read_bytes() == body
    assert not (tmp_path / "source.partial.ts").exists()


@pytest.mark.asyncio
async def test_http_source_download_rejects_length_mismatch(tmp_path: Path) -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            headers={"Content-Length": "100"},
            content=b"short",
        )
    )
    target = tmp_path / "source.ts"

    with pytest.raises(SourceDownloadError, match="length mismatch"):
        await HttpSourceDownloader(transport).download(
            "http://media.test/source.ts", target
        )

    assert not target.exists()
    assert not (tmp_path / "source.partial.ts").exists()


@pytest.mark.asyncio
async def test_http_source_download_rejects_http_error(tmp_path: Path) -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(404, content=b"not found")
    )
    target = tmp_path / "source.ts"

    with pytest.raises(SourceDownloadError, match="HTTP 404"):
        await HttpSourceDownloader(transport).download(
            "https://media.test/missing.ts", target
        )

    assert not target.exists()
