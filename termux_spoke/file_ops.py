"""
Async HTTP download/upload operations for the Termux Spoke.

Matches the upload endpoint in pc_hub/server.py (lines 89-122) and
the download/upload patterns from pc_hub/local_worker.py.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Callable, Optional

import httpx

from termux_spoke import config

logger = logging.getLogger(__name__)


async def download_source_video(
    hub_url: str,
    dest_path: str,
    job_id: str,
    progress_callback: Optional[Callable[[int, int], None]] = None,
    timeout: float = 300.0,
) -> str:
    """
    Download the source video from the PC Hub via HTTP GET.

    Args:
        hub_url: Base URL of the hub (e.g. ``http://192.168.1.100:8000``).
        dest_path: Local filesystem path to save the video.
        job_id: Job identifier (for logging).
        progress_callback: Optional async callback ``(downloaded_bytes, total_bytes)``.
        timeout: Total timeout in seconds.

    Returns:
        The absolute path of the downloaded file.

    Raises:
        httpx.HTTPStatusError: On non-2xx response.
        httpx.TimeoutException: On timeout.
    """
    url = f"{hub_url}{config.DOWNLOAD_PATH}"
    logger.info("[%s] Downloading source video from %s", job_id, url)

    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout)) as client:
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            total = int(response.headers.get("content-length", 0))

            os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)
            downloaded = 0
            with open(dest_path, "wb") as f:
                async for chunk in response.aiter_bytes():
                    f.write(chunk)
                    downloaded += len(chunk)
                    if progress_callback and total > 0:
                        progress_callback(downloaded, total)

    total_mb = downloaded / (1024 * 1024)
    logger.info("[%s] Downloaded %.2f MB to %s", job_id, total_mb, dest_path)
    return os.path.abspath(dest_path)


async def upload_chunk(
    hub_url: str,
    task_id: str,
    file_path: str,
    speed_multiplier: float,
    worker_id: str,
    timeout: float = 120.0,
) -> bool:
    """
    Upload a completed encoded chunk to the PC Hub via multipart POST.

    Matches the ``POST /upload/{task_id}`` endpoint in ``pc_hub/server.py`` (lines 89-122)
    which expects: ``file``, ``speed_multiplier``, ``worker_id`` form fields.

    Also mirrors ``pc_hub/local_worker.py`` ``_upload_file()`` behaviour.

    Returns:
        ``True`` on success.

    Raises:
        httpx.HTTPStatusError: On non-2xx response.
        httpx.TimeoutException: On timeout.
        FileNotFoundError: If the chunk file does not exist.
    """
    url = f"{hub_url}{config.UPLOAD_PATH.format(task_id=task_id)}"

    file_path = os.path.abspath(file_path)
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"Chunk file not found: {file_path}")

    file_size = os.path.getsize(file_path)
    logger.info(
        "Uploading %s (%d bytes, %.2fx speed) to %s",
        task_id, file_size, speed_multiplier, url,
    )

    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout)) as client:
        with open(file_path, "rb") as f:
            files = {
                "file": (f"{task_id}_encoded.mp4", f, "video/mp4"),
            }
            data = {
                "speed_multiplier": str(speed_multiplier),
                "worker_id": worker_id,
            }
            response = await client.post(url, files=files, data=data)
            response.raise_for_status()

    logger.info("Upload %s successful", task_id)
    return True


async def check_hub_health(hub_url: str, timeout: float = 5.0) -> bool:
    """
    Check if the PC Hub is reachable and healthy.

    Returns:
        ``True`` if the hub responds with HTTP 200 and ``{"status": "ok"}``.
    """
    url = f"{hub_url}{config.HEALTH_PATH}"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout)) as client:
            response = await client.get(url)
            if response.status_code == 200:
                data = response.json()
                return data.get("status") == "ok"
            return False
    except (httpx.HTTPError, httpx.TimeoutException, ValueError) as exc:
        logger.debug("Health check failed: %s", exc)
        return False


async def resolve_hub_url(host: str, port: int = config.DEFAULT_PORT) -> tuple[str, str]:
    """
    Build the HTTP and WebSocket URLs from host/port and verify reachability.

    Returns:
        ``(http_url, ws_url)`` e.g. ``("http://192.168.1.100:8000", "ws://192.168.1.100:8000")``

    Raises:
        ConnectionError: If the hub is not reachable.
    """
    http_url = f"http://{host}:{port}"
    ws_url = f"ws://{host}:{port}"

    healthy = await check_hub_health(http_url)
    if not healthy:
        # Try once more after a brief delay
        await asyncio.sleep(1)
        healthy = await check_hub_health(http_url)

    if not healthy:
        raise ConnectionError(f"PC Hub at {http_url} is not reachable or unhealthy")

    return http_url, ws_url