"""
Beautiful Terminal UI for the Termux FFmpeg Distributed Spoke.

Uses the Rich library to render a live-updating 5-section dashboard
with Catppuccin Mocha dark theme.

Layout:
    ┌─ Header ──────────────────────────────────────────────┐
    │  🎬 FFMPEG DISTRIBUTED SPOKE (TERMUX)                 │
    ├─ Connection Panel ────────────────────────────────────┤
    │  ● Status: Connected  |  Hub: 192.168.1.100:8000     │
    │  ● Health: OK, 3 workers  |  Discovery: Auto          │
    ├─ Worker State Panel ──────────────────────────────────┤
    │  Worker: abc12345...  |  Status: ● ENCODING           │
    │  Task: chunk_00005  |  Segment: 5.1s → 9.9s (4.8s)  │
    │  ████████████████░░░░░░  67.3%  |  2.45x @ 234 FPS   │
    ├─ Log Console ─────────────────────────────────────────┤
    │  HH:MM:SS TAG │ Message text here...                  │
    │  HH:MM:SS TAG │ Another log line...                   │
    ├─ Footer ──────────────────────────────────────────────┤
    │  [Q]uit  [M]anual IP  [R]econnect  [D]ebug           │
    └───────────────────────────────────────────────────────┘
"""
from __future__ import annotations

import asyncio
import logging
import sys
from typing import Optional

from rich.align import Align
from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn
from rich.style import Style
from rich.table import Table
from rich.text import Text

from termux_spoke import config
from termux_spoke.client import SpokeClient, SpokeState

logger = logging.getLogger(__name__)


# ── Style Helpers ───────────────────────────────────────────


# Pre-built rich styles for performance
_STYLE_TEXT = Style(color=config.TEXT)
_STYLE_DIM = Style(color=config.SUBTEXT0)
_STYLE_GREEN = Style(color=config.GREEN)
_STYLE_YELLOW = Style(color=config.YELLOW)
_STYLE_RED = Style(color=config.RED)
_STYLE_BLUE = Style(color=config.BLUE)
_STYLE_LAVENDER = Style(color=config.LAVENDER)
_STYLE_TEAL = Style(color=config.TEAL)
_STYLE_PEACH = Style(color=config.PEACH)
_STYLE_MAUVE = Style(color=config.MAUVE)
_STYLE_FLAMINGO = Style(color=config.FLAMINGO)
_STYLE_SUBTEXT0 = Style(color=config.SUBTEXT0)
_STYLE_HEADER = Style(color=config.TEXT, bold=True)

# Tag styles for log console
_TAG_STYLES: dict[str, Style] = {
    "DEBUG": Style(color=config.SURFACE1),
    "INFO": Style(color=config.SUBTEXT1, bold=True),
    "WARN": Style(color=config.YELLOW, bold=True),
    "CONNECT": Style(color=config.BLUE, bold=True),
    "DISCOVER": Style(color=config.MAUVE, bold=True),
    "REGISTER": Style(color=config.LAVENDER, bold=True),
    "TASK": Style(color=config.YELLOW, bold=True),
    "DOWNLOAD": Style(color=config.TEAL, bold=True),
    "CACHE": Style(color=config.FLAMINGO, bold=True),
    "ENCODE": Style(color=config.YELLOW, bold=True),
    "PROGRESS": Style(color=config.SUBTEXT0),
    "UPLOAD": Style(color=config.GREEN, bold=True),
    "ERROR": Style(color=config.RED, bold=True),
    "ABORT": Style(color=config.PEACH, bold=True),
    "PROTOCOL": Style(color=config.SURFACE1),
}

# Status colors (dot + text)
_STATUS_COLORS: dict[str, tuple[str, str]] = {
    "Disconnected": (config.RED, config.RED),
    "Connecting": (config.YELLOW, config.YELLOW),
    "Connected": (config.YELLOW, config.YELLOW),
    "Downloading": (config.TEAL, config.TEAL),
    "Encoding": (config.YELLOW, config.YELLOW),
    "Uploading": (config.GREEN, config.GREEN),
    "Registered": (config.GREEN, config.GREEN),
    "Busy": (config.YELLOW, config.YELLOW),
    "Idle": (config.GREEN, config.GREEN),
}


# ── TUI Class ───────────────────────────────────────────────


class SpokeTui:
    """
    Manages the Rich Live TUI display with a 5-section layout.

    The TUI runs in an asyncio task, refreshing the display periodically
    and handling keyboard input.
    """

    def __init__(self, client: SpokeClient) -> None:
        self.client = client
        self.console = Console()
        self.layout = Layout()
        self._setup_layout()

        # Log display buffer (last N entries to show)
        self._max_log_lines = 15

        # Keyboard input state
        self._manual_ip_requested = False
        self._manual_ip_value: Optional[str] = None
        self._reconnect_requested = False
        self._debug_mode = False
        self._shutdown_requested = False

        # Track previous state for detecting meaningful changes
        self._last_state: Optional[SpokeState] = None

        # Live instance (set during run)
        self._live: Optional[Live] = None

    def _setup_layout(self) -> None:
        """Build the 5-section Rich Layout."""
        self.layout.split_column(
            Layout(name="header", size=3),
            Layout(name="connection", size=5),
            Layout(name="worker_state", size=6),
            Layout(name="logs", ratio=1),
            Layout(name="footer", size=2),
        )

    def _render_header(self) -> Panel:
        """Render the app title header."""
        title = Text("🎬  FFMPEG DISTRIBUTED SPOKE  (TERMUX)", style=_STYLE_HEADER)
        subtitle = Text(
            "Distributed video encoding worker • Catppuccin Mocha",
            style=_STYLE_DIM,
        )
        content = Group(
            Align.center(title),
            Align.center(subtitle),
        )
        return Panel(
            content,
            border_style=config.SURFACE0,
            padding=(0, 1),
            style=config.BASE,
        )

    def _render_connection_panel(self, state: SpokeState) -> Panel:
        """Render the PC Hub connection status panel."""
        status = state.connection_status or "Disconnected"
        dot_color, text_color = _STATUS_COLORS.get(status, (config.RED, config.RED))
        dot = Text("●", style=dot_color)
        status_text = Text(f" {status}", style=text_color)

        # Status line
        line1 = Group(
            dot,
            status_text,
            Text("  │  Hub: ", style=_STYLE_DIM),
            Text(f"{state.hub_host}:{state.hub_port}", style=_STYLE_BLUE)
            if state.hub_host
            else Text("—", style=_STYLE_DIM),
        )

        # Second line
        method_str = state.discovery_method or "—"
        worker_str = state.worker_id[:12] + "..." if state.worker_id else "—"
        health_str = state.hub_health or "—"

        line2 = Text.assemble(
            ("Worker ID: ", _STYLE_DIM),
            (worker_str, _STYLE_MAUVE),
            ("  │  Discovery: ", _STYLE_DIM),
            (method_str, _STYLE_TEAL),
            ("  │  Health: ", _STYLE_DIM),
            (health_str, _STYLE_GREEN),
        )

        content = Group(line1, "", line2)
        return Panel(
            content,
            title="[bold]PC HUB CONNECTION[/]",
            border_style=config.SURFACE0,
            padding=(1, 2),
            style=config.CRUST,
        )

    def _render_worker_panel(self, state: SpokeState) -> Panel:
        """Render the worker state and encoding progress."""
        status = state.connection_status or "Disconnected"
        dot_color, text_color = _STATUS_COLORS.get(status, (config.RED, config.RED))

        # Status badge
        badge = Text.assemble(
            ("● ", dot_color),
            (status, text_color),
        )

        # Worker ID
        wid = state.worker_id[:16] + "..." if state.worker_id else "Not registered"
        worker_info = Text.assemble(
            ("Worker: ", _STYLE_DIM),
            (wid, _STYLE_MAUVE),
            ("  │  ", _STYLE_DIM),
            ("Tasks: ", _STYLE_DIM),
            (str(state.tasks_completed), _STYLE_GREEN),
            (" / ", _STYLE_DIM),
            (str(state.tasks_failed), _STYLE_RED),
        )

        # Task info
        if state.current_task_id:
            task_info = Text.assemble(
                ("Task: ", _STYLE_DIM),
                (state.current_task_id, _STYLE_YELLOW),
                ("  │  ", _STYLE_DIM),
                ("Segment: ", _STYLE_DIM),
                (f"{state.segment_start:.1f}s → ", config.TEXT),
                (f"{state.segment_start + state.segment_duration:.1f}s", config.TEXT),
                ("  (", _STYLE_DIM),
                (f"{state.segment_duration:.2f}s", config.PEACH),
                (")", _STYLE_DIM),
            )
        else:
            task_info = Text("No active task", style=_STYLE_DIM)

        # Build progress bar
        progress = Progress(
            SpinnerColumn(finished_text="✓", style=config.GREEN),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(
                complete_style=config.BLUE,
                finished_style=config.GREEN,
                pulse_style=config.SURFACE0,
            ),
            TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
            TextColumn("• {task.fields[speed]}"),
            TextColumn("• {task.fields[fps]}"),
            expand=True,
        )
        if state.current_task_id:
            pct = min(state.progress_percent, 100.0)
            progress.add_task(
                f"Encoding {state.current_task_id[:16]}",
                total=100,
                completed=pct,
                speed=f"{state.speed:.2f}x" if state.speed else "—",
                fps=f"{state.fps:.0f} FPS" if state.fps else "—",
            )
        else:
            progress.add_task("Idle", total=100, completed=0, speed="—", fps="—")

        # Uptime
        uptime_str = self._format_uptime(state.uptime_seconds) if state.uptime_seconds else "—"
        uptime = Text.assemble(
            ("Uptime: ", _STYLE_DIM),
            (uptime_str, config.SUBTEXT0),
        )

        content = Group(badge, worker_info, task_info, "", progress, "", uptime)
        return Panel(
            content,
            title="[bold]WORKER STATE[/]",
            border_style=config.SURFACE0,
            padding=(1, 2),
            style=config.CRUST,
        )

    def _render_logs_panel(self, state: SpokeState) -> Panel:
        """Render the scrollable log console."""
        table = Table.grid(padding=(0, 1), expand=True)

        # Show only the last N log entries
        logs = state.logs[-self._max_log_lines:] if state.logs else []
        if self._debug_mode:
            logs = state.logs  # show all if in debug mode

        if not logs:
            table.add_row(Text("Waiting for activity...", style=_STYLE_DIM))
        else:
            for timestamp, tag, message in logs:
                tag_style = _TAG_STYLES.get(tag, _STYLE_DIM)
                line = Text.assemble(
                    (timestamp, _STYLE_DIM),
                    (" ", _STYLE_DIM),
                    (f"{tag:8}", tag_style),
                    (" │ ", _STYLE_DIM),
                    (message, _STYLE_TEXT),
                )
                table.add_row(line)

        return Panel(
            table,
            title="[bold]CONSOLE LOGS[/]",
            border_style=config.SURFACE0,
            padding=(1, 2),
            style=config.MANTLE,
        )

    def _render_footer(self) -> Panel:
        """Render the keyboard shortcut footer."""
        shortcuts = Text.assemble(
            ("  [Q] ", config.PEACH),
            ("uit    ", _STYLE_DIM),
            ("[M] ", config.PEACH),
            ("anual IP    ", _STYLE_DIM),
            ("[R] ", config.PEACH),
            ("econnect    ", _STYLE_DIM),
            ("[D] ", config.PEACH),
            ("ebug", _STYLE_DIM),
        )
        return Panel(
            shortcuts,
            border_style=config.SURFACE0,
            padding=(0, 2),
            style=config.BASE,
        )

    def _build_layout(self, state: SpokeState) -> Layout:
        """Build the full layout from the current state."""
        self.layout["header"].update(self._render_header())
        self.layout["connection"].update(self._render_connection_panel(state))
        self.layout["worker_state"].update(self._render_worker_panel(state))
        self.layout["logs"].update(self._render_logs_panel(state))
        self.layout["footer"].update(self._render_footer())
        return self.layout

    @staticmethod
    def _format_uptime(seconds: float) -> str:
        """Format uptime seconds into HH:MM:SS."""
        hours, remainder = divmod(int(seconds), 3600)
        minutes, secs = divmod(remainder, 60)
        if hours > 0:
            return f"{hours}h {minutes}m {secs}s"
        elif minutes > 0:
            return f"{minutes}m {secs}s"
        else:
            return f"{secs}s"

    # ── Main Loop ───────────────────────────────────────────

    async def run(self) -> None:
        """
        Run the TUI main loop.

        This enters the Rich ``Live`` context which renders the dashboard,
        periodically refreshes from the client state, and handles keyboard input.

        When manual IP is requested (``m`` key), the Live context is exited,
        the user is prompted for input, then Live resumes.
        """
        self.client.set_on_state_change(self._notify)

        while not self._shutdown_requested:
            # Use Live with screen=True for full terminal rendering
            with Live(
                self.layout,
                console=self.console,
                refresh_per_second=4,
                screen=True,
                auto_refresh=False,
            ) as live:
                self._live = live

                # Start stdin reader for keyboard input
                stdin_task = asyncio.create_task(self._stdin_reader())

                try:
                    while not self._shutdown_requested and not self._manual_ip_requested:
                        state = self.client.get_state()
                        self._build_layout(state)
                        live.refresh()

                        if self._reconnect_requested:
                            self._reconnect_requested = False
                            if state.hub_host:
                                asyncio.create_task(
                                    self.client.connect(
                                        state.hub_host,
                                        state.hub_port,
                                        state.discovery_method or "manual",
                                    )
                                )

                        await asyncio.sleep(config.TUI_REFRESH_INTERVAL)

                except asyncio.CancelledError:
                    pass
                finally:
                    stdin_task.cancel()
                    try:
                        await stdin_task
                    except asyncio.CancelledError:
                        pass

            # If manual IP was requested, prompt the user
            if self._manual_ip_requested and not self._shutdown_requested:
                self._manual_ip_requested = False
                await self._prompt_manual_ip()

    async def _prompt_manual_ip(self) -> None:
        """Prompt the user for a manual hub IP address outside of Live mode."""
        self.console.print()
        self.console.print(
            "[bold]Enter PC Hub IP address[/] (or leave blank to cancel): ",
            end="",
        )
        try:
            ip = await asyncio.get_event_loop().run_in_executor(
                None, sys.stdin.readline
            )
            ip = ip.strip()
            if ip:
                await self.client.connect(ip, config.DEFAULT_PORT, method="manual")
            else:
                self.client.add_log("CONNECT", "Manual IP entry cancelled")
        except Exception as exc:
            self.client.add_log("ERROR", f"Manual IP failed: {exc}")

    def _notify(self) -> None:
        """Called by client when state changes — triggers a TUI refresh."""
        # The Live loop will pick up the new state on its next iteration
        pass

    # ── Keyboard Input ──────────────────────────────────────

    async def _stdin_reader(self) -> None:
        """
        Read single keypresses from stdin non-blockingly.

        Handles:
        - ``q`` / ``Q``: Request shutdown
        - ``m`` / ``M``: Request manual IP entry
        - ``r`` / ``R``: Request reconnect
        - ``d`` / ``D``: Toggle debug mode
        """
        loop = asyncio.get_event_loop()
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        await loop.connect_read_pipe(lambda: protocol, sys.stdin)

        while not self._shutdown_requested:
            try:
                char = await reader.readexactly(1)
                char = char.decode("utf-8", errors="replace").lower()

                if char == "q":
                    self._shutdown_requested = True
                    asyncio.create_task(self.client.shutdown())
                elif char == "m":
                    self._manual_ip_requested = True
                elif char == "r":
                    self._reconnect_requested = True
                elif char == "d":
                    self._debug_mode = not self._debug_mode

            except asyncio.IncompleteReadError:
                break
            except Exception:
                break

    @property
    def shutdown_requested(self) -> bool:
        return self._shutdown_requested

    @property
    def manual_ip_requested(self) -> bool:
        return self._manual_ip_requested