# 🎬 FFmpeg Distributed Video Encoder

> **⚠️ Project now archived.** This codebase is preserved for reference. No further development or maintenance is planned.

> **Distributed video encoding across your local network** — split any video into chunks, encode them in parallel across multiple PCs and Android devices, then seamlessly merge the results.

[![Python](https://img.shields.io/badge/Python-3.12+-3776AB?logo=python&logoColor=white)](https://python.org)
[![Kotlin](https://img.shields.io/badge/Kotlin-Android-7F52FF?logo=kotlin&logoColor=white)](https://kotlinlang.org)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

---

## Table of Contents

- [Architecture Overview](#architecture-overview)
- [System Requirements](#system-requirements)
- [Project Structure](#project-structure)
- [Components](#components)
  - [PC Hub (Coordinator)](#-pc-hub-coordinator)
  - [Termux Spoke (Python Worker)](#-termux-spoke-python-worker)
  - [Android Spoke (Kotlin Worker)](#-android-spoke-kotlin-worker)
- [Protocol](#protocol)
- [Getting Started](#getting-started)
  - [PC Hub Setup](#pc-hub-setup)
  - [Termux Spoke Setup](#termux-spoke-setup)
  - [Android APK Build](#android-apk-build)
- [Usage Guide](#usage-guide)
  - [PC Hub UI Walkthrough](#pc-hub-ui-walkthrough)
  - [Termux TUI Walkthrough](#termux-tui-walkthrough)
  - [Encoding Configuration](#encoding-configuration)
- [Development](#development)
  - [Running Tests](#running-tests)
  - [Adding Features](#adding-features)
- [FAQ / Troubleshooting](#faq--troubleshooting)

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                          LOCAL NETWORK                           │
│                                                                  │
│  ┌──────────────────────┐          ┌──────────────────────────┐  │
│  │     PC HUB (Coordinator)        │   Workers (Spokes)       │  │
│  │                      │          │                          │  │
│  │  ┌─────────────────┐ │          │  ┌────────────────────┐  │  │
│  │  │ FastAPI Server   │──WebSocket──│ Termux Spoke (Python)│  │  │
│  │  │ (ws://:8000/ws)  │◄─────────►│  (on Android via Termux)│  │  │
│  │  └─────────────────┘ │  HTTP    │  └────────────────────┘  │  │
│  │         │            │  POST    │                          │  │
│  │  ┌─────────────────┐ │  upload  │  ┌────────────────────┐  │  │
│  │  │ PySide6 GUI      │ │          │  │ Android Spoke (Kotlin)│  │
│  │  │ (Desktop App)    │ │          │  │  (native APK)         │  │
│  │  └─────────────────┘ │          │  └────────────────────┘  │  │
│  │         │            │          │                          │  │
│  │  ┌─────────────────┐ │          │  ┌────────────────────┐  │  │
│  │  │ Local PC Worker  │─ ─ ─ ─ ─ ─│ Local PC can also      │  │
│  │  │ (on Hub machine)  │          │  │ participate as worker │  │
│  │  └─────────────────┘ │          │  └────────────────────┘  │  │
│  │                      │          │                          │  │
│  │  ┌─────────────────┐ │          │  Service Discovery:       │  │
│  │  │ mDNS Advertiser  │◄─────────►│  _ffmpeg-hub._tcp.local.  │  │
│  │  │ (Zeroconf)       │ │          │  (via python-zeroconf)   │  │
│  │  └─────────────────┘ │          │                          │  │
│  └──────────────────────┘          └──────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
```

### How It Works

1. **Split & Distribute**: The PC Hub takes a source video, divides it into time-based chunks, and assigns each chunk to an available worker.

2. **Encode in Parallel**: Each worker (PC, Android phone, or Termux device) downloads the source video once per job, then encodes its assigned segment using FFmpeg with task-specific arguments.

3. **Progress Reporting**: Workers stream real-time progress — percentage, speed (x), and FPS — back to the hub via WebSocket.

4. **Collect & Merge**: As chunks complete, workers upload their encoded segments to the hub via HTTP POST. The hub tracks all chunks, and when done, concatenates them using FFmpeg's concat demuxer, optionally muxing with a separately-extracted audio track.

### Key Design Decisions

- **Source video is served once, cached per worker**: The hub serves the full source video as a static download. Each worker caches it locally so subsequent chunk assignments within the same job reuse the local copy — no redundant downloads.
- **Audio extracted separately on the hub PC**: In "separate audio" mode (default), audio is extracted once on the hub PC before encoding begins. Video chunks are encoded without audio (`-an`), and the final merge muxes the clean audio track back in. This avoids audio artifacts from splitting/concatenating compressed audio.
- **Dynamic chunk sizing**: Chunk duration adapts per worker based on their historical encoding speed — faster workers get larger chunks, slower workers smaller ones. This keeps all workers busy without stragglers.
- **Worker-agnostic protocol**: The protocol (WebSocket registration, task assignment, progress reporting, HTTP upload) is identical for all worker types — Python, Kotlin, or any future implementation.

---

## System Requirements

| Role | Platform | Requirements |
|------|----------|-------------|
| **PC Hub** | Windows / macOS / Linux | Python 3.12+, FFmpeg, PySide6 |
| **Termux Spoke** | Android (Termux) | Python 3.12+, FFmpeg (`pkg install ffmpeg`), Rich TUI |
| **Android Spoke** | Android | APK (Kotlin, API 24+, ~4MB) |
| **Network** | LAN (Wi-Fi) | Devices must be on the same subnet for mDNS discovery |

---

## Project Structure

```
ffmpeg-parallel-v3/
│
├── pc_hub/                          # PC Hub — coordinator + GUI
│   ├── main.py                      # PySide6 desktop application
│   ├── server.py                    # FastAPI WebSocket server + upload endpoints
│   ├── discovery.py                 # mDNS/Zeroconf service advertiser
│   ├── local_worker.py              # Optional local PC worker thread
│   └── requirements.txt
│
├── termux_spoke/                    # Termux Spoke — Python worker client
│   ├── __main__.py                  # Entry point with CLI args + signal handling
│   ├── client.py                    # Async WebSocket client orchestrator
│   ├── config.py                    # Catppuccin Mocha palette + protocol constants
│   ├── discovery.py                 # Zeroconf hub service browser
│   ├── ffmpeg_runner.py             # Async FFmpeg subprocess with progress parsing
│   ├── file_ops.py                  # Async HTTP download/upload helpers
│   ├── tui.py                       # Rich TUI dashboard (live dashboard)
│   ├── setup_and_run.sh             # Termux auto-setup + launcher script
│   └── requirements.txt
│
├── android_spoke/                   # Android Spoke — native Kotlin app
│   ├── app/build.gradle             # Module build config
│   ├── build.gradle                 # Top-level build config
│   ├── settings.gradle
│   ├── gradle.properties
│   └── app/src/main/java/com/ffmpegparallel/spoke/
│       ├── MainActivity.kt          # UI + lifecycle (programmatic layout)
│       ├── EncodingService.kt       # Foreground service: WS, ffmpeg-kit, upload
│       └── NsdHelper.kt             # Android NSD discovery for PC Hub
│
├── tests/                           # pytest unit tests
│   ├── conftest.py                  # sys.path configuration
│   ├── test_discovery.py            # HubBrowser tests
│   ├── test_ffmpeg_runner.py        # Progress parsing, command building
│   ├── test_file_ops.py             # Download/upload/health check tests
│   └── test_protocol.py             # Client state + protocol messages
│
├── build_apk_portable.ps1           # Windows build script (portable JDK+SDK+Gradle)
├── pyproject.toml                   # Python package configuration
├── AGENTS.md                        # Dev notes (SSH access to Termux device)
└── README.md                        # This file
```

---

## 🖥️ PC Hub (Coordinator)

The PC Hub is the **central coordinator** — it runs a FastAPI WebSocket server, a PySide6 desktop GUI, and an optional local worker. It manages the entire encoding lifecycle.

### `pc_hub/server.py` — FastAPI WebSocket Server

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/health` | GET | Returns `{"status": "ok", "workers": N}` |
| `/ws` | WebSocket | Worker registration + real-time task assignment |
| `/download/source` | GET | Serves the source video file to workers |
| `/upload/{task_id}` | POST | Receives completed encoded chunks |
| `/download/` | Static | Mounted chunk directory (future use) |

**WebSocket message flow** (see [Protocol](#protocol) for full detail):

```
Worker                                Hub
  │                                    │
  │  {"type": "register", ...}         │
  │ ──────────────────────────────────►│
  │  {"type": "registered", ...}       │
  │ ◄──────────────────────────────────│
  │  {"type": "task_assign", ...}      │
  │ ◄──────────────────────────────────│
  │  {"type": "progress_update", ...}  │
  │ ──────────────────────────────────►│
  │  [HTTP POST /upload/{task_id}]     │
  │ ──────────────────────────────────►│
```

### `pc_hub/main.py` — Desktop GUI

The **PySide6 GUI** provides:

- **Server Control**: Start/stop the FastAPI server and mDNS discovery
- **Job Configuration**: Input file, output path, FFmpeg arguments, audio mode, PC worker toggle
- **Worker Monitor**: Live cards for each connected worker showing name, status (IDLE/BUSY), progress bar, encoding speed
- **Overall Progress**: Aggregate progress bar + elapsed time + chunk completion count
- **Console Log**: Scrollable log output

### `pc_hub/discovery.py` — mDNS Advertiser

Registers a `_ffmpeg-hub._tcp.local.` Zeroconf service on the local network so workers can auto-discover the hub without manual IP entry.

### `pc_hub/local_worker.py` — Local PC Worker

An **optional worker thread** that runs on the hub machine itself. It reuses the `WebSocket` + `HTTP upload` protocol like any remote worker, but has direct file access so it can skip downloading the source video and read it from the local filesystem. This is useful when the hub PC has spare CPU cores.

---

## 📱 Termux Spoke (Python Worker)

A full-featured **Termux-native Python client** that runs in Termux on Android devices. It features a **Rich TUI dashboard** with Catppuccin Mocha dark theme.

### `termux_spoke/__main__.py` — Entry Point

```
python -m termux_spoke                        # Auto-discovery mode
python -m termux_spoke --manual 192.168.1.10  # Manual IP mode
python -m termux_spoke --manual 192.168.1.10 --port 8000
python -m termux_spoke --verbose              # Debug logging
```

Sets up:
- Logging (file + TUI buffer, no stderr spew)
- Signal handlers (SIGINT/SIGTERM → graceful shutdown)
- Zeroconf discovery or direct connection
- Rich TUI dashboard

### `termux_spoke/client.py` — SpokeClient

The central orchestrator. Lifecycle:

```
DISCOVER → CONNECT → REGISTER → LISTEN → TASK → ENCODE → UPLOAD → LISTEN
```

Key features:
- **WebSocket reconnection** with exponential backoff (5s → 30s max, 5 attempts)
- **Source video caching** (caches per job, evicts oldest when > `MAX_CACHED_JOBS`)
- **Task guard** (`_task_active` flag prevents concurrent task assignments)
- **State snapshot** via `get_state()` for TUI rendering
- **Progress callback** bridges `FfmpegRunner` output to WebSocket progress messages

### `termux_spoke/ffmpeg_runner.py` — Async FFmpeg Runner

Runs FFmpeg as a subprocess with `-progress pipe:1` and parses stdout in real time.

**Hang detection** uses a **watchdog task pattern** (not `asyncio.wait_for`):
- On Android, pipe `readline()` can block the event loop thread, making `wait_for` unreliable
- A separate watchdog coroutine monitors `_last_progress_time` and kills the process if stalled
- Two timeout phases: `FFMPEG_STARTUP_GRACE` (180s — Android ffmpeg can be slow to start) and `FFMPEG_HANG_TIMEOUT` (60s — mid-encode stall)

Output file path is created via `os.makedirs`. On failure, stderr is captured (last 500 chars). The runner supports cancellation via `abort()`.

### `termux_spoke/tui.py` — Rich TUI Dashboard

Live-updating dashboard layout:

```
┌─ Header ──────────────────────────────────────────────┐
│  🎬 FFMPEG DISTRIBUTED SPOKE (TERMUX)                 │
├─ Connection Panel ────────────────────────────────────┤
│  ● Status: Registered  |  Hub: 192.168.1.100:8000     │
│  Worker ID: abc123... | Discovery: Auto  | Health: OK │
├─ Worker State Panel ──────────────────────────────────┤
│  ● Registered  |  Worker: abc123...                   │
│  Tasks: 5 / 0  |  Task: chunk_00005                   │
│  Segment: 5.1s → 9.9s (4.8s)                         │
│  ████████████████░░░░░░  67.3%  |  2.45x • 234 FPS   │
├─ Console Logs ────────────────────────────────────────┤
│  HH:MM:SS ENCODE   │ Encoding chunk_00005...          │
│  HH:MM:SS UPLOAD   │ Uploaded 4.2 MB                  │
├─ Footer ──────────────────────────────────────────────┤
│  [Q]uit  [M]anual IP  [R]econnect  [D]ebug            │
└───────────────────────────────────────────────────────┘
```

Keyboard shortcuts (live, no Enter needed):
| Key | Action |
|-----|--------|
| `q` | Shutdown spoke gracefully |
| `m` | Enter manual hub IP address |
| `r` | Reconnect to last hub |
| `d` | Toggle debug log mode (show all logs) |

### `termux_spoke/discovery.py` — Hub Browser

Uses `python-zeroconf`'s `ServiceBrowser` to find `_ffmpeg-hub._tcp.` services on the LAN (the inverse of the PC Hub's advertiser). Handles service add/remove/update events. Falls back gracefully if network is unavailable.

### `termux_spoke/setup_and_run.sh` — Auto-Setup Script

Idempotent bash script for Termux:
1. Creates work directory
2. Installs `python`, `ffmpeg`, `termux-api` via `pkg`
3. Copies spoke source files
4. Creates Python virtualenv and installs dependencies
5. Configures hub IP (CLI arg, saved config, or interactive prompt)
6. Acquires Termux wake lock (prevents Doze mode from killing the process)
7. Launches the spoke
8. Releases wake lock on exit

Usage:
```bash
bash setup_and_run.sh                          # auto-discovery
bash setup_and_run.sh --manual 192.168.1.100   # manual IP
```

---

## 🤖 Android Spoke (Kotlin Worker)

A native Android app built with Kotlin, OkHttp, and FFmpegKit. It runs as a **foreground service** with a persistent notification, allowing encoding to continue even when the app is backgrounded.

### Architecture

| Component | File | Purpose |
|-----------|------|---------|
| `MainActivity.kt` | UI | Programmatic dark-theme layout with connection panel, status, and scrollable logs |
| `EncodingService.kt` | Foreground Service | WebSocket, source download, FFmpeg-kit encoding, chunk upload |
| `NsdHelper.kt` | Discovery | Android NSD (Network Service Discovery) to find PC Hub |

### `EncodingService.kt` — Foreground Service Lifecycle

1. **Service starts** → foreground notification ("Searching for PC Hub...")
2. **User binds** → NSD discovery begins (or manual IP entered)
3. **Hub found** → WebSocket connect → register → listen for tasks
4. **Task received** → download source (cached per job) → FFmpegKit encode with progress → HTTP upload → done
5. **Service destroyed** → cleanup cache, close WebSocket

Key implementation details:
- Uses **OkHttp WebSocket** (not raw Java WebSocket) with zero read/write timeouts
- Uses **FFmpegKit (`ffmpeg-kit-lts-16kb` 6.1.7)** with `sync execute()` to respect sequential per-job ordering
- Progress via `FFmpeKitConfig.enableStatisticsCallback` → WebSocket `progress_update`
- Caches one source video at a time (invalidates when `jobId` changes)
- `ffmpeg_args_to_list()` regex parser matches the Python version in `termux_spoke/ffmpeg_runner.py`

### `NsdHelper.kt` — Android NSD

Discover `_ffmpeg-hub._tcp.` services using Android's built-in NSD (Network Service Discovery). Resolves discovered services and triggers auto-connection. Strips IPv6 zone indexes (`%wlan0`) for clean host addresses.

### Building the APK

**Windows** — use the portable build script:
```powershell
.\build_apk_portable.ps1
```
This downloads JDK 17, Android SDK Commandline Tools, and Gradle 8.5 into a local `android_build_env/` directory, then builds the APK. No pre-installed toolchain required.

**Manual** — with Android Studio or pre-installed SDK:
```bash
cd android_spoke
./gradlew assembleDebug
# APK at: android_spoke/app/build/outputs/apk/debug/app-debug.apk
```

---

## Protocol

### WebSocket Message Types

All messages are JSON-encoded. The WebSocket endpoint is `ws://<hub>:8000/ws`.

#### Registration (Worker → Hub)
```json
{
  "type": "register",
  "name": "Worker Name",
  "is_local_pc": false
}
```

#### Registration Acknowledged (Hub → Worker)
```json
{
  "type": "registered",
  "worker_id": "uuid-v4-string"
}
```

#### Task Assignment (Hub → Worker)
```json
{
  "type": "task_assign",
  "task_id": "chunk_00001",
  "job_id": "job-uuid",
  "video_url": "http://192.168.1.100:8000/download/source",
  "local_video_path": "/full/path/to/source.mp4",
  "start_time": 5.120,
  "duration": 4.760,
  "ffmpeg_args": "-c:v libx264 -preset fast -crf 23 -an"
}
```

- `video_url`: HTTP URL for downloading the source video (all workers)
- `local_video_path`: Local filesystem path (local PC workers skip download)
- `start_time` / `duration`: Segment boundaries in seconds
- `ffmpeg_args`: FFmpeg codec options; `-an` is appended automatically in separate-audio mode

#### Progress Update (Worker → Hub)
```json
{
  "type": "progress_update",
  "task_id": "chunk_00001",
  "percent": 67.3,
  "speed": 2.45,
  "fps": 30.0
}
```
Sent periodically (throttled to 0.5s intervals). `percent` is `(processed_time / segment_duration) * 100`.

#### Error (Worker → Hub)
```json
{
  "type": "error",
  "task_id": "chunk_00001",
  "reason": "FFmpeg exited with code 1"
}
```
Triggers the hub to re-queue the chunk (treated as task completion with 0.0 speed).

#### Abort (Hub → Worker)
```json
{
  "type": "abort"
}
```
Broadcast to all workers when the user clicks "Abort Job". Workers kill their current FFmpeg process.

### HTTP Upload

Completed chunks are uploaded to the hub via multipart POST:

```
POST /upload/{task_id}
Content-Type: multipart/form-data

Fields:
- file: binary (the encoded .mp4 chunk)
- speed_multiplier: float (e.g., "2.45")
- worker_id: string
```

### Chunk File Naming

- On disk: `{output_chunks_dir}/{task_id}_encoded.mp4`
- Upload endpoint: `POST /upload/{task_id}`
- The hub saves the file as `{task_id}_encoded.mp4`

---

## Getting Started

### PC Hub Setup

#### Prerequisites
- **Python 3.12+** ([python.org](https://python.org))
- **FFmpeg** in PATH ([ffmpeg.org](https://ffmpeg.org))

#### Install & Run

```bash
# Navigate to project root
cd ffmpeg-parallel-v3

# Create virtual environment (optional but recommended)
python -m venv venv
# Windows:
venv\Scripts\activate
# macOS/Linux:
# source venv/bin/activate

# Install dependencies
pip install -r pc_hub/requirements.txt

# Launch the PC Hub GUI
python -m pc_hub.main
```

The GUI will open. Click **"Start Hub Server"** to begin accepting worker connections. The hub's IP address and port (default `8000`) are displayed.

### Termux Spoke Setup

#### Method 1: Auto-Setup Script (recommended)

```bash
# Copy termux_spoke/ to your Android device (via SCP, USB, or git clone)
# Then in Termux:
cd path/to/termux_spoke
bash setup_and_run.sh
```

The script will install all dependencies, prompt for the PC Hub IP (or leave blank for auto-discovery), and launch.

#### Method 2: Manual Setup

```bash
# Install system packages
pkg update -y
pkg install -y python ffmpeg termux-api

# Create and activate virtual environment
python -m venv venv
source venv/bin/activate

# Install Python dependencies
pip install -r termux_spoke/requirements.txt

# Run the spoke
python -m termux_spoke
# Or with manual IP:
python -m termux_spoke --manual 192.168.1.100
```

#### Pushing Files to Termux

Using `scp` (as configured in `AGENTS.md`):
```bash
scp termux_spoke/*.py u0_a258@192.168.1.85:~/ffmpeg-spoke/termux_spoke/
scp termux_spoke/requirements.txt u0_a258@192.168.1.85:~/ffmpeg-spoke/
```

SSH into the Termux device:
```bash
ssh u0_a258@192.168.1.85 -p8022
```

### Android APK Build

```powershell
# Windows — portable build (downloads JDK, SDK, Gradle automatically)
.\build_apk_portable.ps1
```

Or build manually with Android Studio:
1. Open `android_spoke/` in Android Studio
2. Sync Gradle
3. Run → Select device or build APK

The APK will be at `android_spoke/app/build/outputs/apk/debug/app-debug.apk`.

---

## Usage Guide

### PC Hub UI Walkthrough

1. **Start the Server**: Click "Start Hub Server". The IP address displays in the UI. Workers can now connect.

2. **Configure the Job**:
   - **Input Video**: Browse or type the path to your source video (MP4, MKV, MOV, AVI supported)
   - **Output Save**: Destination for the final encoded video
   - **FFmpeg Arguments**: Codec options (default: `-c:v libx264 -preset fast -crf 23`)
   - **Extract audio separately** (default on): Extracts audio on the hub PC before encoding. Chunks are encoded without audio, then merged with the clean audio track. Uncheck for combined video+audio in chunks (useful if audio sync is critical).
   - **PC participates as worker** (default on): The hub machine also processes chunks using its own CPU cores.

3. **Start Encoding**: Click "Start Encoding". The hub will:
   - Analyze the video duration
   - Extract audio (if separate mode)
   - Register the local PC worker (if enabled)
   - Begin assigning chunks to connected workers

4. **Monitor Progress**:
   - Overall progress bar shows aggregate encoding progress
   - Worker cards show individual chunk progress + encoding speed
   - Console log shows detailed activity

5. **Finish**: When all chunks are encoded, the hub automatically concatenates them. Click **"Show in Explorer"** to reveal the output file.

6. **Abort**: Click "Abort Job" to stop encoding and clean up temporary files. Workers receive an abort signal.

### Termux TUI Walkthrough

After launching `python -m termux_spoke`:

1. **Auto-discovery** will search for the PC Hub on the LAN for 30 seconds
2. If found, it connects automatically and registers as a worker
3. If not found, press `M` to enter the hub's IP address manually
4. Wait for task assignments — progress updates appear in real time
5. Press `Q` to gracefully shut down

The dashboard shows:
- Real-time encoding progress with speed (x) and FPS
- Connection status and hub health
- Task completion/failure counters
- Scrollable log console

### Encoding Configuration

#### Common FFmpeg Presets

| Arguments | Use Case |
|-----------|----------|
| `-c:v libx264 -preset fast -crf 23` | Good quality, fast (default) |
| `-c:v libx264 -preset medium -crf 18` | Higher quality, slower |
| `-c:v libx264 -preset ultrafast -crf 28` | Maximum speed, lower quality |
| `-c:v libx265 -preset medium -crf 28` | H.265/HEVC, smaller files |
| `-c:v libx264 -preset fast -crf 23 -vf scale=1280:-2` | Scale to 720p |

#### Audio Modes

| Mode | Behavior | When to Use |
|------|----------|-------------|
| **Separate** (default) | Audio extracted on hub PC. Chunks encoded with `-an`. Final merge muxes audio back. | Best quality — avoids audio splitting artifacts. Workers don't process audio. |
| **Combined** | Chunks include both video and audio. No separate audio extraction. | Use if you need per-chunk audio (e.g., for custom segment-based processing). |

---

## Development

### Running Tests

```bash
# From project root
pytest

# With coverage (install pytest-cov first)
pytest --cov=termux_spoke --cov=pc_hub

# Run a specific test file
pytest tests/test_ffmpeg_runner.py -v

# Run tests matching a pattern
pytest -k "split_args or progress"
```

### Test Structure

| Test File | Coverage |
|-----------|----------|
| `test_discovery.py` | HubBrowser init, start/stop, service found/lost callbacks |
| `test_ffmpeg_runner.py` | `_split_args()`, command building, progress parsing, edge cases |
| `test_file_ops.py` | Upload, download, health check with mocked HTTP |
| `test_protocol.py` | SpokeState, SpokeClient initial state, logs, state changes |

### Adding Features

#### Adding a New Worker Type

1. Implement the WebSocket protocol:
   - Connect to `ws://<hub>:8000/ws`
   - Send `{"type": "register", "name": "...", "is_local_pc": false}`
   - Listen for `task_assign` messages
2. On task assignment:
   - Download source from `video_url` (or use `local_video_path` if on the hub)
   - Run FFmpeg with: `ffmpeg -y -ss {start_time} -i {input} -t {duration} {ffmpeg_args} {output}`
   - Send `progress_update` messages via the WebSocket (throttled to ~0.5s)
   - Upload completed chunk to `POST /upload/{task_id}`
3. See `pc_hub/local_worker.py` (Python) or `EncodingService.kt` (Kotlin) for reference implementations.

#### Adding Server Endpoints

Edit `pc_hub/server.py` — add FastAPI routes using the standard decorator pattern. The `state` object provides access to workers, tasks, and directories. UI callbacks can be wired via `state.on_*` hooks.

#### Customizing the TUI Theme

Edit `termux_spoke/config.py` — all Catppuccin Mocha colors are defined as module-level constants. The TUI and config use these for consistent theming across components.

---

## FAQ / Troubleshooting

### Workers can't find the PC Hub

- **Check firewall**: Port 8000 (or your configured port) must be open on the hub machine
- **Check subnet**: All devices must be on the same local network subnet
- **Check mDNS**: Windows Firewall may block Zeroconf/mDNS. Try using manual IP mode instead
- **Manual IP**: On the spoke, press `M` and enter the hub's IP directly

### Encoding fails with "FFmpeg exited with code 1"

- Check the FFmpeg arguments for typos or unsupported codecs
- Ensure workers have the required codecs installed (e.g., `libx264` requires a build of FFmpeg with x264 support)
- On Termux: `pkg install ffmpeg` includes x264 by default
- The last 500 characters of FFmpeg's stderr are captured and logged

### Chunk merging fails

- **Separate audio mode**: If the source video has no audio track, audio extraction will fail silently. Combined mode still works.
- **Disk space**: Ensure sufficient free space in the workspace directories. The hub creates `workspace_temp/` and `workspace_output_chunks/` in its working directory.
- **File permissions**: The merge process writes to the output file path you specified.

### Slow encoding on Android

- Android devices have limited CPU power compared to desktop machines
- Use `-preset ultrafast` for quicker (but larger) encodes
- The hub's dynamic chunk sizing will adapt to each worker's speed — slower workers get smaller chunks
- On Termux, the `termux-wake-lock` prevents Doze mode, but thermal throttling still applies

### WebSocket disconnections during long encodes

- Workers have exponential reconnect backoff (5s → 30s, 5 attempts)
- In-progress tasks are abandoned and re-queued by the hub
- Source video is cached per job ID, so reconnected workers don't re-download

### "ffmpeg not found" errors

- **PC Hub**: Install FFmpeg from [ffmpeg.org](https://ffmpeg.org) and ensure it's in your PATH
- **Termux**: Run `pkg install ffmpeg`
- **Android APK**: FFmpeg is bundled via the `ffmpeg-kit-lts-16kb` dependency

---

## License

MIT License — see the [LICENSE](LICENSE) file for details.

---

## Project Status

**Archived.** This project is no longer actively developed. The codebase is preserved for reference.

The architecture supports three worker types (PC Hub local worker, Termux Python spoke, Android Kotlin app) with a shared, extensible protocol. Planned but uncompleted features include:

- GPU-accelerated encoding (NVIDIA NVENC, Intel QSV, AMD AMF)
- Web-based hub management dashboard
- S3-compatible cloud chunk storage
- Variable chunk overlap for seamless transitions
- Worker capability negotiation (hardware, codecs, max resolution)