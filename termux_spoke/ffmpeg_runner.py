"""
Async FFmpeg subprocess runner with ``-progress pipe:1`` stdout parsing.

Matches the progress-parsing pattern in ``pc_hub/local_worker.py`` (lines 133-258)
and the ffmpeg command construction in ``EncodingService.kt`` (lines 232-242).

Hang detection uses a **watchdog task** rather than ``asyncio.wait_for``
on ``readline()``, because on Android (Termux) pipe reads can block the
event loop thread, preventing ``wait_for`` from ever firing.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import time
from dataclasses import dataclass
from typing import Callable, Optional

from termux_spoke import config


class HangError(Exception):
    """Raised when ffmpeg produces no output for longer than the hang timeout."""


class CancelledByWatchdog(Exception):
    """Raised when the watchdog watchdog task detects a hang."""

logger = logging.getLogger(__name__)

# Regex to extract key=value lines from ffmpeg -progress pipe output
# Lines look like: "out_time_us=5120000", "speed=  2.45x", "fps=45.7"
_PROGRESS_RE = re.compile(r"^([a-zA-Z_]+)=([^\n]*)$")



@dataclass
class FfmpegProgress:
    """Parsed progress from ffmpeg stderr/stdout output."""

    time_us: int = 0
    speed: float = 0.0
    fps: float = 0.0
    percent: float = 0.0
    bitrate: float = 0.0
    total_size: int = 0


@dataclass
class FfmpegResult:
    """Result of a completed FFmpeg encoding run."""

    task_id: str
    success: bool
    return_code: int
    final_speed: float = 0.0
    final_fps: float = 0.0
    output_path: str = ""
    error_message: str = ""


class FfmpegRunner:
    """
    Manages a single ffmpeg subprocess for encoding one chunk.

    Command structure (identical to the PC Hub's task assignments):
        ffmpeg -y -ss {start_time:.3f} -i {input} -t {duration:.3f} \\
               {ffmpeg_args} -progress pipe:1 {output}

    Progress is parsed from stdout (via ``-progress pipe:1``) and yielded
    as an async iterator of ``FfmpegProgress`` objects.
    """

    def __init__(
        self,
        task_id: str,
        input_path: str,
        output_path: str,
        start_time: float,
        duration: float,
        ffmpeg_args: str,
        *,
        hang_timeout: float = config.FFMPEG_HANG_TIMEOUT,
        binary: str = "ffmpeg",
    ) -> None:
        self.task_id = task_id
        self.input_path = os.path.abspath(input_path)
        self.output_path = os.path.abspath(output_path)
        self.start_time = start_time
        self.duration = duration
        self.ffmpeg_args = ffmpeg_args
        self.hang_timeout = hang_timeout
        self.binary = shutil.which(binary) or binary

        self._process: Optional[asyncio.subprocess.Process] = None
        self._cancelled = False
        self._start_time_mono = 0.0
        self._last_progress_time = 0.0
        self._last_report_time = 0.0
        self._last_heartbeat_time = 0.0
        self._got_first_progress = False
        self._watchdog_triggered = False

        # Final stats captured at end
        self._final_speed: float = 0.0
        self._final_fps: float = 0.0

    def _build_command(self) -> list[str]:
        """Build the ffmpeg command-line argument list."""
        cmd = [
            self.binary,
            "-y",
            "-ss", f"{self.start_time:.3f}",
            "-i", self.input_path,
            "-t", f"{self.duration:.3f}",
        ]
        # Parse ffmpeg_args string into individual arguments (handles quoted strings)
        cmd.extend(_split_args(self.ffmpeg_args))
        cmd.extend(["-progress", "pipe:1", self.output_path])
        return cmd

    def _parse_progress_line(self, line: str, state: dict) -> bool:
        """
        Parse a single ``-progress pipe:1`` line and update the state dict.

        Returns ``True`` if this line indicates ``progress=end``.
        """
        stripped = line.strip()
        if not stripped:
            return False

        match = _PROGRESS_RE.match(stripped)
        if not match:
            return False

        key, value = match.group(1), match.group(2)
        value = value.strip()

        if key == "out_time_us":
            state["time_us"] = int(value)
        elif key == "out_time":
            # Some ffmpeg builds emit out_time=HH:MM:SS.xxxxxx
            # parse it as fallback
            try:
                parts = value.split(":")
                if len(parts) == 3:
                    h, m, s = parts
                    state["time_us"] = int(
                        (int(h) * 3600 + int(m) * 60 + float(s)) * 1_000_000
                    )
            except (ValueError, IndexError):
                pass
        elif key == "speed":
            # speed lines: "speed=  2.45x"  or "speed= -0.000x"
            speed_str = value.rstrip("xX").strip()
            try:
                state["speed"] = max(0.0, float(speed_str))
            except ValueError:
                pass
        elif key == "fps":
            try:
                state["fps"] = float(value)
            except ValueError:
                pass
        elif key == "bitrate":
            try:
                state["bitrate"] = float(value)
            except ValueError:
                pass
        elif key == "total_size":
            try:
                state["total_size"] = int(value)
            except ValueError:
                pass
        elif key == "progress":
            return value.strip() == "end"

        return False

    def _compute_percent(self, time_us: int) -> float:
        """Compute encoding progress percentage from time (in microseconds)."""
        if self.duration <= 0:
            return 0.0
        time_sec = time_us / 1_000_000.0
        return min(100.0, (time_sec / self.duration) * 100.0)

    async def run(
        self,
        progress_callback: Optional[Callable[[FfmpegProgress], None]] = None,
    ) -> FfmpegResult:
        """
        Run ffmpeg, parse progress, and return the result.

        ``progress_callback`` is called periodically (throttled to
        ``config.PROGRESS_INTERVAL``) with the latest ``FfmpegProgress``.

        Hang detection uses ``asyncio.wait_for()`` on each ``readline()`` so
        that a stalled ffmpeg process is detected even when no output arrives.
        """
        if not os.path.exists(self.input_path):
            return FfmpegResult(
                task_id=self.task_id, success=False, return_code=-1,
                error_message=f"Input file not found: {self.input_path}",
            )

        command = self._build_command()
        cmd_str = " ".join(command)
        logger.info("[%s] Running: %s", self.task_id, cmd_str)
        logger.info("[%s] Output: %s", self.task_id, self.output_path)

        # Ensure output directory exists
        os.makedirs(os.path.dirname(self.output_path) or ".", exist_ok=True)

        self._last_progress_time = time.monotonic()
        self._start_time_mono = time.monotonic()
        self._last_heartbeat_time = time.monotonic()

        try:
            self._process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            return FfmpegResult(
                task_id=self.task_id, success=False, return_code=-1,
                error_message=(
                    f"ffmpeg not found at '{self.binary}'. "
                    "Install with: pkg install ffmpeg"
                ),
            )

        assert self._process.stdout is not None

        state: dict = {"time_us": 0, "speed": 0.0, "fps": 0.0, "bitrate": 0.0, "total_size": 0}

        try:
            return_code = await self._run_with_hang_detection(state, progress_callback)

        except (HangError, CancelledByWatchdog) as exc:
            self._kill_process()
            logger.warning("[%s] %s", self.task_id, exc)
            return FfmpegResult(
                task_id=self.task_id, success=False, return_code=-1,
                error_message=str(exc),
            )
        except asyncio.CancelledError:
            self._kill_process()
            return FfmpegResult(
                task_id=self.task_id, success=False, return_code=-1,
                error_message="Task cancelled",
            )

        success = return_code == 0 and os.path.exists(self.output_path)

        if success:
            logger.info("[%s] Encoding completed (%.2fx speed)", self.task_id, self._final_speed)
        else:
            await self._capture_stderr()
            logger.warning("[%s] FFmpeg exited with code %d", self.task_id, return_code)

        error_msg = ""
        if not success:
            error_msg = self._stderr_output or f"FFmpeg exited with code {return_code}"

        return FfmpegResult(
            task_id=self.task_id,
            success=success,
            return_code=return_code,
            final_speed=self._final_speed,
            final_fps=self._final_fps,
            output_path=self.output_path,
            error_message=error_msg,
        )

    async def _run_with_hang_detection(
        self,
        state: dict,
        progress_callback: Optional[Callable[[FfmpegProgress], None]],
    ) -> int:
        """
        Read ffmpeg progress lines with a watchdog task for hang detection.

        Uses a **watchdog task** instead of ``asyncio.wait_for(readline())``
        because on Android pipes the readline can block the event loop thread,
        making ``wait_for``'s timeout never fire.

        The watchdog periodically checks ``_last_progress_time`` against the
        current wall clock. If no progress line has arrived within the timeout,
        it sets ``_watchdog_triggered`` and cancels the reader task.

        There are two timeouts:
        - ``FFMPEG_STARTUP_GRACE`` (180s) for the first progress line (Android
          ffmpeg can take minutes to initialize).
        - ``FFMPEG_HANG_TIMEOUT`` (60s) for all subsequent progress lines.
        """
        assert self._process is not None
        assert self._process.stdout is not None

        self._last_progress_time = time.monotonic()
        self._got_first_progress = False
        self._watchdog_triggered = False

        async def _reader() -> None:
            """Read ffmpeg stdout lines and parse progress."""
            while True:
                try:
                    line = await self._process.stdout.readline()
                except Exception:
                    break

                if not line:
                    break

                decoded = line.decode("utf-8", errors="replace").rstrip("\r\n")

                is_end = self._parse_progress_line(decoded, state)
                now = time.monotonic()

                if is_end:
                    self._final_speed = state.get("speed", 0.0)
                    self._final_fps = state.get("fps", 0.0)
                    break

                self._last_progress_time = now
                if not self._got_first_progress:
                    self._got_first_progress = True
                    logger.info("[%s] First progress line received after %.1fs",
                                self.task_id, now - self._start_time_mono)

                # Throttle progress callbacks
                if progress_callback and (now - self._last_report_time) >= config.PROGRESS_INTERVAL:
                    self._last_report_time = now
                    prog = FfmpegProgress(
                        time_us=state.get("time_us", 0),
                        speed=state.get("speed", 0.0),
                        fps=state.get("fps", 0.0),
                        percent=self._compute_percent(state.get("time_us", 0)),
                        bitrate=state.get("bitrate", 0.0),
                        total_size=state.get("total_size", 0),
                    )
                    progress_callback(prog)

        async def _watchdog() -> None:
            """Monitor the reader — raise HangError if it stalls."""
            while True:
                await asyncio.sleep(2.0)

                if self._watchdog_triggered or self._cancelled:
                    return

                now = time.monotonic()
                elapsed = now - self._last_progress_time

                if self._got_first_progress:
                    timeout = self.hang_timeout
                else:
                    timeout = config.FFMPEG_STARTUP_GRACE

                if elapsed >= timeout:
                    logger.warning(
                        "[%s] No progress for %.0fs (had_first_progress=%s) — triggering hang kill",
                        self.task_id, elapsed, self._got_first_progress,
                    )
                    self._watchdog_triggered = True
                    self._kill_process()
                    return

                # Heartbeat: log periodically so the TUI shows the spoke is alive
                if (now - self._last_heartbeat_time) >= config.FFMPEG_HEARTBEAT_INTERVAL:
                    self._last_heartbeat_time = now
                    if not self._got_first_progress:
                        logger.info(
                            "[%s] FFmpeg initializing... (waiting %.0fs of %.0fs grace)",
                            self.task_id, elapsed, timeout,
                        )
                    elif elapsed > 5.0:
                        logger.info(
                            "[%s] Encoding, last progress %.0fs ago",
                            self.task_id, elapsed,
                        )

        reader_task = asyncio.create_task(_reader())
        watchdog_task = asyncio.create_task(_watchdog())

        done, pending = await asyncio.wait(
            [reader_task, watchdog_task],
            return_when=asyncio.FIRST_COMPLETED,
        )

        # Cancel whichever is still running
        for task in pending:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

        # If watchdog won, the reader might still be alive — kill it
        if reader_task in done and reader_task.exception() is None and not reader_task.cancelled():
            pass  # reader finished naturally or by reaching EOF/progress=end
        else:
            # reader was cancelled or raised — check if watchdog triggered
            if self._watchdog_triggered:
                raise HangError(
                    f"FFmpeg hung (no progress for "
                    f"{'startup grace' if not self._got_first_progress else 'encoding'} "
                    f"period)"
                )

        # Wait for process to fully exit
        return await self._process.wait()

    _stderr_output: str = ""

    async def _capture_stderr(self) -> None:
        """Read accumulated stderr from the finished process, if any."""
        if not self._process or not self._process.stderr or self._process.returncode is None:
            return
        try:
            data = await self._process.stderr.read()
            text = data.decode("utf-8", errors="replace").strip()
            if len(text) > 500:
                text = "..." + text[-500:]
            self._stderr_output = text
        except Exception:
            pass

    def abort(self) -> None:
        """Signal the runner to abort the current encoding."""
        self._cancelled = True
        self._kill_process()

    def _kill_process(self) -> None:
        """Terminate the ffmpeg subprocess."""
        if self._process and self._process.returncode is None:
            logger.info("[%s] Killing ffmpeg process (PID %d)", self.task_id, self._process.pid)
            try:
                self._process.kill()
            except ProcessLookupError:
                pass  # already dead

    # ── Utility ─────────────────────────────────────────────────


def _split_args(args_str: str) -> list[str]:
    """
    Split an ffmpeg arguments string into a list, respecting quoted substrings.

    Handles: ``-c:v libx264 -preset fast -an``
    Also: ``-vf "scale=1280:720"``  (double-quoted)
    Also: ``-vf 'scale=1280:720'``  (single-quoted)

    Matches the pattern used in ``EncodingService.kt`` (lines 326-340).
    """
    result: list[str] = []
    # Pattern matches: unquoted tokens, double-quoted strings, single-quoted strings
    pattern = re.compile(r'[^\s"\']+|"([^"]*)"|\'([^\']*)\'')
    for m in re.finditer(pattern, args_str):
        token = m.group(0)
        # Strip surrounding quotes if present
        if len(token) >= 2 and token[0] == token[-1] and token[0] in ('"', "'"):
            result.append(token[1:-1])
        else:
            result.append(token)
    return result