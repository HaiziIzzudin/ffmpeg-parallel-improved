"""
Entry point for the Termux-native FFmpeg Distributed Spoke.

Usage:
    python -m termux_spoke                          # auto-discovery
    python -m termux_spoke --manual 192.168.1.100   # manual IP
    python -m termux_spoke --manual 192.168.1.100 --port 8000

Wires together:
    - ``SpokeClient`` — async WebSocket orchestrator
    - ``SpokeTui`` — Rich Live TUI dashboard
    - ``HubBrowser`` — Zeroconf discovery

Lifecycle:
    1. Parse CLI args
    2. Start TUI (in background task)
    3. Start discovery or connect directly
    4. Run main event loop until shutdown
    5. Clean shutdown on SIGINT/SIGTERM or 'q' key
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from typing import Optional
# Ensure the package's parent directory is on sys.path so
# ``from termux_spoke import ...`` resolves correctly regardless
# of how this script is invoked (``-m termux_spoke``, ``python __main__.py``, etc.).
_pkg_parent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _pkg_parent not in sys.path:
    sys.path.insert(0, _pkg_parent)

from termux_spoke import config
from termux_spoke.client import SpokeClient
from termux_spoke.discovery import HubBrowser
from termux_spoke.tui import SpokeTui


# ── Logging Setup ───────────────────────────────────────────

# Mapping from Python log levels to TUI tag names
_LOG_LEVEL_TO_TAG: dict[int, str] = {
    logging.DEBUG: "DEBUG",
    logging.INFO: "INFO",
    logging.WARNING: "WARN",
    logging.ERROR: "ERROR",
    logging.CRITICAL: "ERROR",
}


class TuiLogHandler(logging.Handler):
    """
    Custom logging handler that routes Python log messages into the
    SpokeClient TUI log buffer so they appear in the Rich dashboard
    instead of spewing raw text to stderr (which breaks the TUI).

    Uses a re-entrancy guard to prevent infinite loops: if a log
    message originates from ``client.add_log()`` (which itself calls
    ``logger.debug()``), the handler silently skips it.
    """

    def __init__(self, level: int = logging.NOTSET) -> None:
        super().__init__(level)
        self._client: Optional[SpokeClient] = None
        self._processing = False

    def set_client(self, client: SpokeClient) -> None:
        """Inject the client reference (created after logging setup)."""
        self._client = client

    def emit(self, record: logging.LogRecord) -> None:
        """Called for every log record — route it to the TUI buffer."""
        if self._processing or self._client is None:
            return
        self._processing = True
        try:
            msg = self.format(record)
            tag = _LOG_LEVEL_TO_TAG.get(record.levelno, "DEBUG")
            # Strip the Python module prefix so TUI message is clean
            # e.g. "termux_spoke.file_ops: Downloading..." -> "Downloading..."
            if ": " in msg:
                msg = msg.split(": ", 1)[1]
            self._client.add_log(tag, msg)
        finally:
            self._processing = False


def _setup_logging(verbose: bool = False) -> tuple[TuiLogHandler, logging.FileHandler]:
    """Configure logging to file + TUI buffer (no stderr spew).

    Returns (tui_handler, file_handler) so the TUI handler can be wired
    to the SpokeClient later.
    """
    log_dir = os.path.join(os.path.expanduser("~"), config.TEMP_DIR_NAME, "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, "spoke.log")

    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

    file_handler = logging.FileHandler(log_file, mode="a")
    file_handler.setFormatter(logging.Formatter(fmt))

    tui_handler = TuiLogHandler(level=level)
    # TUI handler format: no timestamp (TUI adds its own), just level + module + message
    tui_handler.setFormatter(logging.Formatter("[%(levelname)s] %(name)s: %(message)s"))

    logging.basicConfig(
        level=level,
        handlers=[
            file_handler,
            tui_handler,
        ],
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("zeroconf").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)

    logging.info("Logging to %s", log_file)
    return tui_handler, file_handler


# ── Argument Parsing ────────────────────────────────────────


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="FFmpeg Distributed Spoke — Termux-native worker client",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python -m termux_spoke                         Auto-discover hub\n"
            "  python -m termux_spoke --manual 192.168.1.10   Connect directly\n"
        ),
    )
    parser.add_argument(
        "--manual",
        type=str,
        default="",
        metavar="IP",
        help="PC Hub IP address (skip auto-discovery)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=config.DEFAULT_PORT,
        help=f"PC Hub port (default: {config.DEFAULT_PORT})",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable verbose debug logging",
    )
    return parser.parse_args(argv)


# ── Signal Handling ─────────────────────────────────────────


def _setup_signal_handlers(loop: asyncio.AbstractEventLoop, shutdown_event: asyncio.Event) -> None:
    """Set up signal handlers for graceful shutdown."""

    def _handle_signal() -> None:
        if not shutdown_event.is_set():
            logging.info("Received shutdown signal")
            shutdown_event.set()

    try:
        loop.add_signal_handler(signal.SIGINT, _handle_signal)
        loop.add_signal_handler(signal.SIGTERM, _handle_signal)
    except NotImplementedError:
        # Windows doesn't support add_signal_handler; fallback to default behavior
        pass


# ── Manual IP Prompt (fallback from TUI) ────────────────────


async def main(argv: list[str] | None = None) -> int:
    """Main entry point."""
    args = _parse_args(argv)
    tui_handler, _file_handler = _setup_logging(verbose=args.verbose)

    # Event to signal shutdown
    shutdown_event = asyncio.Event()

    # Create client and TUI
    client = SpokeClient()
    tui_handler.set_client(client)  # route Python loggers into TUI
    tui = SpokeTui(client)

    # Signal handling
    loop = asyncio.get_event_loop()
    _setup_signal_handlers(loop, shutdown_event)

    # Start TUI in background
    tui_task = asyncio.create_task(tui.run())

    try:
        browser: Optional[HubBrowser] = None

        if args.manual:
            # Manual IP mode — connect directly
            client.add_log("CONNECT", f"Manual IP mode: {args.manual}:{args.port}")
            await client.connect(args.manual, args.port, method="manual")
        else:
            # Auto-discovery mode — start Zeroconf browser
            client.add_log("DISCOVER", "Starting Zeroconf discovery...")

            hub_found = asyncio.Event()
            hub_host: list[str] = []
            hub_port: list[int] = []

            def _on_hub_found(host: str, port: int) -> None:
                if not hub_found.is_set():
                    hub_host.append(host)
                    hub_port.append(port)
                    hub_found.set()

            browser = HubBrowser(on_hub_found=_on_hub_found)
            browser.start()

            # Wait for hub to be discovered (or shutdown)
            discovery_timeout = 30  # seconds
            try:
                await asyncio.wait_for(
                    hub_found.wait(),
                    timeout=discovery_timeout,
                )
                if hub_host:
                    await client.connect(hub_host[0], hub_port[0], method="auto")
            except asyncio.TimeoutError:
                client.add_log(
                    "DISCOVER",
                    f"No hub discovered in {discovery_timeout}s. "
                    "Press [M] for manual IP entry.",
                )

        # Main wait loop — keep running until shutdown is requested
        while not shutdown_event.is_set() and not tui.shutdown_requested:
            await asyncio.sleep(0.5)

    except asyncio.CancelledError:
        pass
    finally:
        # Graceful shutdown
        logging.info("Shutting down...")

        # Stop Zeroconf browser if it was started
        if browser is not None:
            browser.stop()

        await client.shutdown()
        tui_task.cancel()
        try:
            await tui_task
        except asyncio.CancelledError:
            pass

    print("\nSpoke shut down. Goodbye!")
    return 0


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)