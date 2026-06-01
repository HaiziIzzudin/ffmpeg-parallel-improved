"""
Configuration for the Termux-native FFmpeg Distributed Spoke.

Catppuccin Mocha color palette (matches the existing Android app and PC Hub aesthetic).
Protocol constants matching pc_hub/server.py.
"""

from typing import Final

# ── Catppuccin Mocha Color Palette ──────────────────────────
BASE: Final[str] = "#11111b"     # Darkest background
MANTLE: Final[str] = "#181825"   # Slightly lighter bg (console bg)
CRUST: Final[str] = "#1e1e2e"    # Card backgrounds
SURFACE0: Final[str] = "#313244" # Borders, separators
SURFACE1: Final[str] = "#45475a" # Disabled elements
TEXT: Final[str] = "#cdd6f4"     # Primary text
SUBTEXT0: Final[str] = "#a6adc8" # Secondary text
SUBTEXT1: Final[str] = "#bac2de" # Tertiary text
BLUE: Final[str] = "#89b4fa"     # Primary accent, progress bars
LAVENDER: Final[str] = "#b4befe" # Secondary accent
GREEN: Final[str] = "#a6e3a1"    # Success, idle status
YELLOW: Final[str] = "#f9e2af"   # Warning, busy status
RED: Final[str] = "#f38ba8"      # Error, disconnected
TEAL: Final[str] = "#94e2d5"     # Info highlights
PEACH: Final[str] = "#fab387"    # Speed values
MAUVE: Final[str] = "#cba6f7"    # Worker ID display
ROSEWATER: Final[str] = "#f5e0dc" # Bright highlights
FLAMINGO: Final[str] = "#f2cdcd" # Subtle highlights

# ── Protocol Constants ──────────────────────────────────────
SERVICE_TYPE: Final[str] = "_ffmpeg-hub._tcp."
SERVICE_TYPE_LOCAL: Final[str] = "_ffmpeg-hub._tcp.local."
DEFAULT_PORT: Final[int] = 8000
WS_PATH: Final[str] = "/ws"
UPLOAD_PATH: Final[str] = "/upload/{task_id}"
DOWNLOAD_PATH: Final[str] = "/download/source"
HEALTH_PATH: Final[str] = "/health"

# ── Behavior Defaults ───────────────────────────────────────
RECONNECT_DELAY: Final[float] = 5.0        # seconds between reconnect attempts
RECONNECT_MAX_DELAY: Final[float] = 30.0   # max exponential backoff
MAX_RECONNECT_ATTEMPTS: Final[int] = 5     # before re-running discovery
PROGRESS_INTERVAL: Final[float] = 0.5      # seconds between progress updates to hub
TUI_REFRESH_INTERVAL: Final[float] = 0.2   # seconds between TUI refreshes
FFMPEG_STARTUP_GRACE: Final[float] = 180.0  # seconds to wait for first progress line (Android can be slow)
FFMPEG_HANG_TIMEOUT: Final[float] = 60.0    # seconds without progress mid-encode = hang
FFMPEG_HEARTBEAT_INTERVAL: Final[float] = 15.0  # seconds between "still running" heartbeat logs
MAX_CACHED_JOBS: Final[int] = 2            # keep source vids for up to N jobs
TEMP_DIR_NAME: Final[str] = ".ffmpeg_spoke_cache"  # cache dir under $HOME

# ── Device Info ─────────────────────────────────────────────
import platform
DEVICE_NAME: Final[str] = f"Termux Spoke ({platform.node() or 'android'})"