"""
Async WebSocket client orchestrator for the Termux Spoke.

Full protocol implementation matching:
- ``pc_hub/server.py`` (WebSocket endpoint, upload endpoint)
- ``pc_hub/local_worker.py`` (registration, task handling, progress reporting)
- ``EncodingService.kt`` (connect, register, process task, cache, upload)

Lifecycle:
    DISCOVER → CONNECT → REGISTER → LISTEN → TASK → ENCODE → UPLOAD → LISTEN
"""
from __future__ import annotations

import asyncio
import httpx
import json
import logging
import os
import platform
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import websockets
from websockets.exceptions import ConnectionClosed

from termux_spoke import config, file_ops
from termux_spoke.ffmpeg_runner import FfmpegProgress, FfmpegRunner

logger = logging.getLogger(__name__)


# ── State dataclasses ───────────────────────────────────────


@dataclass
class SpokeState:
    """Observable state snapshot for the TUI to render."""

    # Connection
    hub_host: str = ""
    hub_port: int = config.DEFAULT_PORT
    hub_url: str = ""  # http://host:port
    ws_url: str = ""  # ws://host:port
    connection_status: str = "Disconnected"
    discovery_method: str = ""  # "auto" | "manual" | ""
    worker_id: str = ""
    hub_health: str = ""  # e.g. "OK, 3 workers"

    # Task progress
    current_task_id: str = ""
    current_job_id: str = ""
    segment_start: float = 0.0
    segment_duration: float = 0.0
    progress_percent: float = 0.0
    speed: float = 0.0
    fps: float = 0.0
    ffmpeg_args: str = ""

    # Stats
    tasks_completed: int = 0
    tasks_failed: int = 0
    total_data_uploaded_mb: float = 0.0
    uptime_seconds: float = 0.0

    # Log
    logs: list[tuple[str, str, str]] = field(default_factory=list)  # (timestamp, tag, message)

    def to_dict(self) -> dict[str, Any]:
        return {
            "hub_host": self.hub_host,
            "hub_port": self.hub_port,
            "connection_status": self.connection_status,
            "discovery_method": self.discovery_method,
            "worker_id": self.worker_id,
            "hub_health": self.hub_health,
            "current_task_id": self.current_task_id,
            "current_job_id": self.current_job_id,
            "segment_start": self.segment_start,
            "segment_duration": self.segment_duration,
            "progress_percent": self.progress_percent,
            "speed": self.speed,
            "fps": self.fps,
            "ffmpeg_args": self.ffmpeg_args,
            "tasks_completed": self.tasks_completed,
            "tasks_failed": self.tasks_failed,
            "total_data_uploaded_mb": self.total_data_uploaded_mb,
            "uptime_seconds": self.uptime_seconds,
        }


# ── Main Orchestrator ───────────────────────────────────────


class SpokeClient:
    """
    Async WebSocket client that connects to the PC Hub and processes encoding tasks.

    This is the core orchestrator — it manages the full lifecycle: discovery,
    connection, registration, task processing, progress reporting, and upload.

    The TUI interacts with this class via:
    - ``get_state()`` — periodic state snapshot for rendering
    - ``add_log(tag, msg)`` — log entries displayed in the console panel
    - Callbacks set via ``set_on_state_change()``
    """

    def __init__(self) -> None:
        # Network
        self._websocket: Optional[websockets.WebSocketClientProtocol] = None
        self._worker_id: Optional[str] = None
        self._hub_host: str = ""
        self._hub_port: int = config.DEFAULT_PORT
        self._hub_url: str = ""
        self._ws_url: str = ""

        # Task state
        self._current_runner: Optional[FfmpegRunner] = None
        self._current_task_id: Optional[str] = None
        self._current_job_id: Optional[str] = None
        self._ffmpeg_args: str = ""
        self._segment_start: float = 0.0
        self._segment_duration: float = 0.0

        # Cache: job_id -> source file path
        self._source_cache: dict[str, str] = {}

        # Live progress (updated by ffmpeg runner callback)
        self._live_progress_percent: float = 0.0
        self._live_speed: float = 0.0
        self._live_fps: float = 0.0

        # Stats
        self._tasks_completed: int = 0
        self._tasks_failed: int = 0
        self._total_data_uploaded_mb: float = 0.0
        self._start_time: float = time.monotonic()

        # Connection state
        self._connection_status: str = "Disconnected"
        self._running: bool = False
        self._task_active: bool = False  # guard against concurrent task assignments

        # Discovery
        self._discovery_method: str = ""
        self._hub_health: str = ""

        # Log buffer (shared with TUI)
        self._logs: list[tuple[str, str, str]] = []
        self._max_logs: int = 500

        # Listen/reconnect tasks
        self._listen_task: Optional[asyncio.Task] = None
        self._reconnect_attempts: int = 0

        # State change callback
        self._on_state_change: Optional[Callable[[], None]] = None

    # ── Public API ──────────────────────────────────────────

    def set_on_state_change(self, callback: Optional[Callable[[], None]]) -> None:
        """Set a callback that fires whenever the internal state changes."""
        self._on_state_change = callback

    def get_state(self) -> SpokeState:
        """Get a snapshot of the current state for TUI rendering."""
        return SpokeState(
            hub_host=self._hub_host,
            hub_port=self._hub_port,
            hub_url=self._hub_url,
            ws_url=self._ws_url,
            connection_status=self._connection_status,
            discovery_method=self._discovery_method,
            worker_id=self._worker_id or "",
            hub_health=self._hub_health,
            current_task_id=self._current_task_id or "",
            current_job_id=self._current_job_id or "",
            segment_start=self._segment_start,
            segment_duration=self._segment_duration,
            progress_percent=self._live_progress_percent,
            speed=self._live_speed,
            fps=self._live_fps,
            ffmpeg_args=self._ffmpeg_args,
            tasks_completed=self._tasks_completed,
            tasks_failed=self._tasks_failed,
            total_data_uploaded_mb=self._total_data_uploaded_mb,
            uptime_seconds=time.monotonic() - self._start_time,
            logs=list(self._logs),
        )

    def add_log(self, tag: str, message: str) -> None:
        """Add a timestamped log entry. Thread-safe (runs in asyncio loop)."""
        timestamp = time.strftime("%H:%M:%S")
        entry = (timestamp, tag, message)
        self._logs.append(entry)
        if len(self._logs) > self._max_logs:
            self._logs.pop(0)
        logger.debug("[%s] %s", tag, message)
        self._notify_state_change()

    def is_running(self) -> bool:
        return self._running

    # ── Connection Management ───────────────────────────────

    async def connect(self, host: str, port: int = config.DEFAULT_PORT, method: str = "auto") -> None:
        """
        Connect to the PC Hub.

        Steps:
        1. Resolve hub URLs and check health
        2. Open WebSocket
        3. Send registration
        4. Wait for registered acknowledgement
        5. Start listen loop
        """
        self._hub_host = host
        self._hub_port = port
        self._hub_url = f"http://{host}:{port}"
        self._ws_url = f"ws://{host}:{port}"
        self._discovery_method = method
        self._reconnect_attempts = 0

        self._set_connection_status("Connecting")
        self.add_log("CONNECT", f"Connecting to PC Hub at {self._hub_url}...")

        # Health check
        healthy = await file_ops.check_hub_health(self._hub_url)
        if not healthy:
            self.add_log("ERROR", f"Hub at {self._hub_url} is not reachable")
            self._set_connection_status("Disconnected")
            return

        # Get worker count for display
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(f"{self._hub_url}/health")
                if resp.status_code == 200:
                    data = resp.json()
                    self._hub_health = f"OK, {data.get('workers', '?')} workers"
        except Exception:
            self._hub_health = "OK"

        # WebSocket connect
        try:
            ws_url_full = f"{self._ws_url}/ws"
            self.add_log("CONNECT", f"Opening WebSocket to {ws_url_full}")
            self._websocket = await websockets.connect(
                ws_url_full,
                max_size=None,  # Allow large messages
                ping_interval=20,
                ping_timeout=10,
            )
        except (OSError, websockets.WebSocketException) as exc:
            self.add_log("ERROR", f"WebSocket connection failed: {exc}")
            self._set_connection_status("Disconnected")
            return

        # Register
        device_name = f"Termux Spoke ({platform.node() or 'android'})"
        reg_msg = json.dumps({
            "type": "register",
            "name": device_name,
            "is_local_pc": False,
        })
        await self._websocket.send(reg_msg)

        # Wait for registered response
        try:
            response = await self._websocket.recv()
            data = json.loads(response)
            if data.get("type") == "registered":
                self._worker_id = data["worker_id"]
                self.add_log("REGISTER", f"Registered as {self._worker_id[:8]}...")
                self._set_connection_status("Registered")
            else:
                self.add_log("ERROR", f"Unexpected response: {data}")
                await self._websocket.close()
                self._set_connection_status("Disconnected")
                return
        except (json.JSONDecodeError, ConnectionClosed) as exc:
            self.add_log("ERROR", f"Registration failed: {exc}")
            self._set_connection_status("Disconnected")
            return

        # Start the listen loop
        self._running = True
        self._listen_task = asyncio.create_task(self._listen_loop())

    async def disconnect(self) -> None:
        """Gracefully disconnect from the hub."""
        self._running = False

        # Cancel any in-flight encoding
        self._abort_current_task()

        if self._listen_task:
            self._listen_task.cancel()
            try:
                await self._listen_task
            except asyncio.CancelledError:
                pass
            self._listen_task = None

        if self._websocket:
            try:
                await self._websocket.close(code=1000, reason="Spoke shutting down")
            except Exception:
                pass
            self._websocket = None

        self._worker_id = None
        self._current_task_id = None
        self._current_job_id = None
        self._set_connection_status("Disconnected")
        self.add_log("CONNECT", "Disconnected from PC Hub")

    async def shutdown(self) -> None:
        """Full shutdown — disconnect and clean up temp files."""
        await self.disconnect()
        self._cleanup_cache()

    # ── Listen Loop ─────────────────────────────────────────

    async def _listen_loop(self) -> None:
        """
        Main message loop: receive messages from the hub and handle them.

        Handles:
        - ``task_assign`` → start encoding
        - ``abort`` → kill current task
        """
        assert self._websocket is not None

        try:
            while self._running:
                try:
                    message = await self._websocket.recv()
                except ConnectionClosed as exc:
                    self.add_log("ERROR", f"WebSocket connection lost: {exc.code} {exc.reason}")
                    self._set_connection_status("Disconnected")
                    await self._attempt_reconnect()
                    break

                try:
                    data = json.loads(message)
                except json.JSONDecodeError as exc:
                    self.add_log("ERROR", f"Invalid message from hub: {exc}")
                    continue

                msg_type = data.get("type")

                if msg_type == "task_assign":
                    if self._task_active:
                        self.add_log("WARN", f"Already busy — skipping task {data.get('task_id', '?')}")
                    else:
                        self._task_active = True
                        asyncio.create_task(self._handle_task(data))
                elif msg_type == "abort":
                    self._abort_current_task()
                    self.add_log("ABORT", "Received abort signal from PC Hub")
                else:
                    self.add_log("DEBUG", f"Unknown message type: {msg_type}")

        except asyncio.CancelledError:
            pass
        finally:
            self._listen_task = None

    async def _attempt_reconnect(self) -> None:
        """Attempt reconnection with exponential backoff."""
        while self._running and self._reconnect_attempts < config.MAX_RECONNECT_ATTEMPTS:
            delay = min(
                config.RECONNECT_DELAY * (2**self._reconnect_attempts),
                config.RECONNECT_MAX_DELAY,
            )
            self._reconnect_attempts += 1
            self.add_log(
                "CONNECT",
                f"Reconnecting in {delay:.0f}s (attempt {self._reconnect_attempts})...",
            )
            await asyncio.sleep(delay)

            if not self._running:
                return

            await self.connect(self._hub_host, self._hub_port, self._discovery_method)
            if self._connection_status in ("Registered", "Idle"):
                self._reconnect_attempts = 0
                self.add_log("CONNECT", "Reconnected successfully")
                return

        if self._running:
            self.add_log(
                "ERROR",
                f"Failed to reconnect after {self._reconnect_attempts} attempts. "
                "Use [R] to retry or [M] for manual IP.",
            )
            self._set_connection_status("Disconnected")

    # ── Task Handling ───────────────────────────────────────

    async def _handle_task(self, data: dict) -> None:
        """
        Process a single task assignment from the hub.

        Steps:
        1. Extract task parameters
        2. Ensure source video is cached
        3. Run FFmpeg encoding
        4. Upload completed chunk
        5. Report progress/errors via WebSocket
        """
        task_id = data.get("task_id", "")
        job_id = data.get("job_id", "")
        start_time = data.get("start_time", 0.0)
        duration = data.get("duration", 0.0)
        ffmpeg_args = data.get("ffmpeg_args", "")

        self._current_task_id = task_id
        self._current_job_id = job_id
        self._ffmpeg_args = ffmpeg_args
        self._segment_start = start_time
        self._segment_duration = duration

        self._set_connection_status("Busy")
        self.add_log(
            "TASK",
            f"Assigned {task_id} ({start_time:.1f}s - {start_time + duration:.1f}s, "
            f"{duration:.2f}s duration)",
        )

        # ── Prepare directories ──
        cache_dir = os.path.join(
            os.path.expanduser("~"), config.TEMP_DIR_NAME, job_id
        )
        os.makedirs(cache_dir, exist_ok=True)

        source_path = os.path.join(cache_dir, "source.mp4")
        output_path = os.path.join(cache_dir, f"{task_id}_out.mp4")

        try:
            # ── Step 1: Download / use cached source ──
            if job_id in self._source_cache and os.path.exists(self._source_cache[job_id]):
                source_path = self._source_cache[job_id]
                self.add_log("CACHE", f"Using cached source for job {job_id[:8]}...")
            else:
                # Evict old cache if needed
                self._evict_cache_if_needed()

                self._set_connection_status("Downloading")
                self.add_log("DOWNLOAD", f"Downloading source video...")

                # Build the full video URL from hub base + path
                source_url = f"{self._hub_url}{config.DOWNLOAD_PATH}"

                source_path = await file_ops.download_source_video(
                    hub_url=self._hub_url,
                    dest_path=source_path,
                    job_id=job_id,
                )
                self._source_cache[job_id] = source_path
                size_mb = os.path.getsize(source_path) / (1024 * 1024)
                self.add_log("DOWNLOAD", f"Downloaded {size_mb:.1f} MB")

            # ── Step 2: Run FFmpeg ──
            self._set_connection_status("Encoding")
            self.add_log("ENCODE", f"Encoding {task_id}...")

            runner = FfmpegRunner(
                task_id=task_id,
                input_path=source_path,
                output_path=output_path,
                start_time=start_time,
                duration=duration,
                ffmpeg_args=ffmpeg_args,
            )
            self._current_runner = runner

            result = await runner.run(progress_callback=self._on_ffmpeg_progress)

            if not result.success:
                raise RuntimeError(result.error_message)

            self.add_log(
                "ENCODE",
                f"Encoding complete (speed: {result.final_speed:.2f}x, "
                f"fps: {result.final_fps:.0f})",
            )

            # ── Step 3: Upload ──
            self._set_connection_status("Uploading")
            self.add_log("UPLOAD", f"Uploading chunk {task_id}...")

            await file_ops.upload_chunk(
                hub_url=self._hub_url,
                task_id=task_id,
                file_path=output_path,
                speed_multiplier=result.final_speed,
                worker_id=self._worker_id or "",
            )

            uploaded_mb = os.path.getsize(output_path) / (1024 * 1024)
            self._total_data_uploaded_mb += uploaded_mb
            self._tasks_completed += 1
            self.add_log("UPLOAD", f"Uploaded {uploaded_mb:.1f} MB — task complete")

        except Exception as exc:
            self._tasks_failed += 1
            self.add_log("ERROR", f"Task failed: {exc}")
            await self._send_error(task_id, str(exc))

        finally:
            # Cleanup output file
            if os.path.exists(output_path):
                try:
                    os.remove(output_path)
                except OSError:
                    pass

            self._current_runner = None
            self._current_task_id = None
            self._task_active = False
            self._set_connection_status("Registered")

    def _on_ffmpeg_progress(self, progress: FfmpegProgress) -> None:
        """
        Callback fired by FfmpegRunner when progress updates are available.

        Sends ``progress_update`` message to the hub via WebSocket,
        and updates live tracking fields for the TUI.
        """
        self._live_progress_percent = progress.percent
        self._live_speed = progress.speed
        self._live_fps = progress.fps

        if not self._websocket or not self._current_task_id:
            return

        progress_msg = json.dumps({
            "type": "progress_update",
            "task_id": self._current_task_id,
            "percent": round(progress.percent, 1),
            "speed": round(progress.speed, 2),
            "fps": round(progress.fps, 1),
        })

        try:
            # schedule the send in the event loop
            asyncio.ensure_future(self._safe_send(progress_msg))
        except Exception:
            pass

    async def _safe_send(self, message: str) -> None:
        """Send a message over WebSocket, catching disconnection errors."""
        if not self._websocket:
            return
        try:
            await self._websocket.send(message)
        except ConnectionClosed:
            pass

    async def _send_error(self, task_id: str, reason: str) -> None:
        """Report a task error to the hub."""
        if not self._websocket:
            return
        try:
            error_msg = json.dumps({
                "type": "error",
                "task_id": task_id,
                "reason": reason,
            })
            await self._websocket.send(error_msg)
            self.add_log("PROTOCOL", f"Reported error for {task_id}")
        except Exception:
            pass

    def _abort_current_task(self) -> None:
        """Kill the currently running ffmpeg process (if any)."""
        if self._current_runner:
            self._current_runner.abort()
            self._current_runner = None
        self._task_active = False
        self.add_log("ABORT", "Aborted current encoding task")

    # ── Cache Management ────────────────────────────────────

    def _evict_cache_if_needed(self) -> None:
        """Remove oldest cached job source if we've exceeded MAX_CACHED_JOBS."""
        while len(self._source_cache) >= config.MAX_CACHED_JOBS:
            oldest_job = next(iter(self._source_cache))
            oldest_path = self._source_cache.pop(oldest_job)
            if oldest_path and os.path.exists(oldest_path):
                try:
                    os.remove(oldest_path)
                    self.add_log("CACHE", f"Evicted old cache: {oldest_job[:8]}...")
                except OSError:
                    pass

    def _cleanup_cache(self) -> None:
        """Remove all cached source videos."""
        for job_id, path in list(self._source_cache.items()):
            try:
                if path and os.path.exists(path):
                    os.remove(path)
            except OSError:
                pass
        self._source_cache.clear()
        self.add_log("CACHE", "Cleaned up all cached files")

    # ── State Management ────────────────────────────────────

    def _set_connection_status(self, status: str) -> None:
        self._connection_status = status
        self._notify_state_change()

    def _notify_state_change(self) -> None:
        if self._on_state_change:
            try:
                self._on_state_change()
            except Exception:
                pass