"""
Unit tests for the file_ops module — download, upload, health check.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from termux_spoke import file_ops

# ── Internal Helpers ───────────────────────────────────────


def _make_client(attrs: dict) -> AsyncMock:
    """
    Build an AsyncMock that acts like httpx.AsyncClient for ``async with``.

    AsyncMock auto-generates ``__aenter__`` and ``__aexit__``, so
    ``async with client:`` works correctly.

    For async HTTP methods (``get``, ``post``, etc.), pass the mocked
    *response object* directly as the value.  ``_make_client`` wraps it
    in an ``AsyncMock`` so that ``await client.get(url)`` yields the response.

    For ``stream``, pass the async-context-manager ``AsyncMock`` directly
    (``stream()`` is NOT a coroutine in httpx, so no ``AsyncMock`` wrapping).

    For special cases like ``side_effect`` exceptions, pass a pre-built
    ``AsyncMock`` instance instead.
    """
    client = AsyncMock(spec=httpx.AsyncClient)
    client.__aenter__.return_value = client  # ``as c`` → the same mock
    for name, value in attrs.items():
        if name in ("get", "post", "put", "delete", "patch", "head", "options"):
            # These are async methods — mock must be awaitable
            if isinstance(value, AsyncMock):
                setattr(client, name, value)
            else:
                setattr(client, name, AsyncMock(return_value=value))
        elif name == "stream":
            # stream() is NOT a coroutine — it returns an async context manager synchronously.
            # Use a plain MagicMock so calling client.stream(...) returns the CM, not a coroutine.
            setattr(client, "stream", MagicMock(return_value=value))
        else:
            setattr(client, name, value)
    return client


def _healthy_response() -> MagicMock:
    r = MagicMock(spec=httpx.Response)
    r.status_code = 200
    r.json = MagicMock(return_value={"status": "ok", "workers": 3})
    return r


def _unhealthy_response(status: int = 500) -> MagicMock:
    r = MagicMock(spec=httpx.Response)
    r.status_code = status
    return r


# ── Tests ──────────────────────────────────────────────────


class TestUploadChunk:
    """Tests for the upload_chunk function."""

    def test_upload_file_not_found(self) -> None:
        """Upload of non-existent file should raise FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            asyncio.run(
                file_ops.upload_chunk(
                    hub_url="http://192.168.1.100:8000",
                    task_id="chunk_00001",
                    file_path="/nonexistent/file.mp4",
                    speed_multiplier=1.0,
                    worker_id="w1",
                )
            )

    def test_upload_success(self) -> None:
        """Successful upload should return True."""
        mock_response = _healthy_response()
        client = _make_client({"post": mock_response})

        with patch("httpx.AsyncClient", return_value=client):
            with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
                f.write(b"fake encoded chunk data")
                file_path = f.name

            try:
                result = asyncio.run(
                    file_ops.upload_chunk(
                        hub_url="http://192.168.1.100:8000",
                        task_id="chunk_00001",
                        file_path=file_path,
                        speed_multiplier=2.5,
                        worker_id="worker-abc",
                        timeout=30,
                    )
                )
                assert result is True
            finally:
                os.unlink(file_path)

    def test_upload_http_error(self) -> None:
        """HTTP error should raise."""
        mock_response = _unhealthy_response(500)
        mock_response.raise_for_status = MagicMock(
            side_effect=httpx.HTTPStatusError(
                "Server error",
                request=MagicMock(),
                response=mock_response,
            )
        )
        client = _make_client({"post": mock_response})

        with patch("httpx.AsyncClient", return_value=client):
            with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
                f.write(b"data")
                file_path = f.name

            try:
                with pytest.raises(httpx.HTTPStatusError):
                    asyncio.run(
                        file_ops.upload_chunk(
                            hub_url="http://192.168.1.100:8000",
                            task_id="chunk_00001",
                            file_path=file_path,
                            speed_multiplier=1.0,
                            worker_id="w1",
                        )
                    )
            finally:
                os.unlink(file_path)


class TestCheckHubHealth:
    """Tests for the check_hub_health function."""

    def test_healthy(self) -> None:
        """Healthy hub returns True."""
        mock_response = _healthy_response()
        client = _make_client({"get": mock_response})

        with patch("httpx.AsyncClient", return_value=client):
            result = asyncio.run(
                file_ops.check_hub_health("http://192.168.1.100:8000")
            )
            assert result is True

    def test_unhealthy(self) -> None:
        """HTTP error returns False."""
        mock_response = _unhealthy_response(500)
        client = _make_client({"get": mock_response})

        with patch("httpx.AsyncClient", return_value=client):
            result = asyncio.run(
                file_ops.check_hub_health("http://192.168.1.100:8000")
            )
            assert result is False

    def test_wrong_status(self) -> None:
        """Hub returning wrong status returns False."""
        mock_response = _healthy_response()
        mock_response.json = MagicMock(return_value={"status": "error"})
        client = _make_client({"get": mock_response})

        with patch("httpx.AsyncClient", return_value=client):
            result = asyncio.run(
                file_ops.check_hub_health("http://192.168.1.100:8000")
            )
            assert result is False

    def test_connection_error(self) -> None:
        """Connection error returns False."""
        get_mock = AsyncMock(side_effect=httpx.ConnectError("Connection refused"))
        client = _make_client({"get": get_mock})

        with patch("httpx.AsyncClient", return_value=client):
            result = asyncio.run(
                file_ops.check_hub_health("http://192.168.1.100:8000")
            )
            assert result is False


class TestDownloadSourceVideo:
    """Tests for download_source_video."""

    def test_download_success(self) -> None:
        """Successful download should return the file path."""
        chunks = [b"hello", b" ", b"world"]

        # Streaming response mock
        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.headers = {"content-length": "11"}
        mock_response.aiter_bytes = lambda: _async_gen(chunks)

        # stream() returns an async context manager; its __aenter__ yields the response
        stream_cm = AsyncMock()
        stream_cm.__aenter__.return_value = mock_response

        client = _make_client({"stream": stream_cm})

        with patch("httpx.AsyncClient", return_value=client):
            with tempfile.TemporaryDirectory() as tmpdir:
                dest = os.path.join(tmpdir, "source.mp4")
                result = asyncio.run(
                    file_ops.download_source_video(
                        hub_url="http://192.168.1.100:8000",
                        dest_path=dest,
                        job_id="job-abc",
                    )
                )
                assert result == os.path.abspath(dest)
                with open(dest) as f:
                    assert f.read() == "hello world"


def _async_gen(items):
    """Small helper: turn a list into an async generator."""
    async def gen():
        for item in items:
            yield item
    return gen()