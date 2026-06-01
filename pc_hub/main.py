import os
import sys
import json
import time
import uuid
import shutil
import logging
import asyncio
import subprocess
import threading
from typing import List, Dict, Any

from PySide6.QtCore import QObject, Signal, Slot, QThread, QMetaObject, Qt
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QLineEdit, QFileDialog, QCheckBox, QProgressBar,
    QTextEdit, QGroupBox, QFrame, QScrollArea, QSizePolicy
)
from PySide6.QtGui import QFont, QColor, QPalette, QIcon

# Set up logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# Import our server and discovery modules
import server
from server import app, state, set_directories, WorkerSession
from discovery import HubDiscovery, get_local_ip
from local_worker import LocalPCWorker, get_ffmpeg_duration

# Thread-safe signals to communicate from FastAPI thread to PySide6 UI
class HubSignals(QObject):
    worker_registered = Signal(object)      # WorkerSession
    worker_disconnected = Signal(str)       # worker_id
    task_progress = Signal(str, str, float, float, int)  # worker_id, task_id, percent, speed, fps
    task_completed = Signal(str, str, float) # worker_id, task_id, speed
    log_message = Signal(str)                # message string

# Global signals instance
signals = HubSignals()

# Connect FastAPI state hooks to Qt signals
def setup_server_hooks():
    state.on_worker_registered = lambda worker: signals.worker_registered.emit(worker)
    state.on_worker_disconnected = lambda w_id: signals.worker_disconnected.emit(w_id)
    state.on_task_progress = lambda w_id, t_id, pct, spd, fps: signals.task_progress.emit(w_id, t_id, pct, spd, fps)
    state.on_task_completed = lambda w_id, t_id, spd: signals.task_completed.emit(w_id, t_id, spd)

class UvicornServerThread(QThread):
    def __init__(self, host: str, port: int):
        super().__init__()
        self.host = host
        self.port = port
        self.server = None

    def run(self):
        import uvicorn
        config = uvicorn.Config(app, host=self.host, port=self.port, log_level="warning")
        self.server = uvicorn.Server(config)
        
        # Run the server loop
        asyncio.run(self.server.serve())

    def stop(self):
        if self.server:
            self.server.should_exit = True
            self.wait()

class WorkerWidget(QFrame):
    """Custom UI component representing a worker node card."""
    def __init__(self, worker: WorkerSession, parent=None):
        super().__init__(parent)
        self.worker_id = worker.worker_id
        self.name = worker.name
        self.is_local = worker.is_local
        
        self.setFrameShape(QFrame.StyledPanel)
        self.setLineWidth(1)
        self.setStyleSheet("""
            WorkerWidget {
                background-color: #1e1e2e;
                border: 1px solid #313244;
                border-radius: 8px;
                padding: 10px;
            }
        """)
        
        layout = QVBoxLayout(self)
        
        # Header info
        header_layout = QHBoxLayout()
        icon_label = QLabel("💻" if self.is_local else "📱")
        icon_label.setStyleSheet("font-size: 16px;")
        name_label = QLabel(self.name)
        name_label.setStyleSheet("font-weight: bold; color: #cdd6f4;")
        
        self.status_badge = QLabel("IDLE")
        self.status_badge.setStyleSheet("""
            QLabel {
                background-color: #a6e3a1;
                color: #11111b;
                font-size: 10px;
                font-weight: bold;
                border-radius: 4px;
                padding: 2px 6px;
            }
        """)
        
        header_layout.addWidget(icon_label)
        header_layout.addWidget(name_label)
        header_layout.addStretch()
        header_layout.addWidget(self.status_badge)
        layout.addLayout(header_layout)
        
        # Task & Speed details
        self.task_label = QLabel("Task: None")
        self.task_label.setStyleSheet("color: #a6adc8; font-size: 11px;")
        self.speed_label = QLabel("Speed: -")
        self.speed_label.setStyleSheet("color: #89b4fa; font-size: 11px; font-weight: bold;")
        
        details_layout = QHBoxLayout()
        details_layout.addWidget(self.task_label)
        details_layout.addStretch()
        details_layout.addWidget(self.speed_label)
        layout.addLayout(details_layout)
        
        # Progress Bar
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setStyleSheet("""
            QProgressBar {
                background-color: #313244;
                border: none;
                border-radius: 4px;
                height: 12px;
                text-align: center;
                color: #cdd6f4;
                font-size: 9px;
            }
            QProgressBar::chunk {
                background-color: #89b4fa;
                border-radius: 4px;
            }
        """)
        layout.addWidget(self.progress_bar)

    def update_progress(self, task_id: str, percent: float, speed: float, fps: int):
        self.status_badge.setText("BUSY")
        self.status_badge.setStyleSheet("""
            QLabel {
                background-color: #f9e2af;
                color: #11111b;
                font-size: 10px;
                font-weight: bold;
                border-radius: 4px;
                padding: 2px 6px;
            }
        """)
        self.task_label.setText(f"Task: {task_id}")
        self.speed_label.setText(f"{speed:.2f}x ({fps} FPS)")
        self.progress_bar.setValue(int(percent))

    def set_idle(self):
        self.status_badge.setText("IDLE")
        self.status_badge.setStyleSheet("""
            QLabel {
                background-color: #a6e3a1;
                color: #11111b;
                font-size: 10px;
                font-weight: bold;
                border-radius: 4px;
                padding: 2px 6px;
            }
        """)
        self.task_label.setText("Task: None")
        self.progress_bar.setValue(0)

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Antigravity Distributed Video Encoder")
        self.resize(950, 700)
        self.setStyleSheet("""
            QMainWindow {
                background-color: #11111b;
            }
            QLabel {
                color: #cdd6f4;
                font-family: 'Outfit', 'Inter', sans-serif;
            }
            QPushButton {
                background-color: #89b4fa;
                color: #11111b;
                border: none;
                border-radius: 6px;
                padding: 8px 16px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #b4befe;
            }
            QPushButton:disabled {
                background-color: #45475a;
                color: #7f849c;
            }
            QLineEdit {
                background-color: #1e1e2e;
                border: 1px solid #313244;
                border-radius: 6px;
                padding: 6px;
                color: #cdd6f4;
            }
            QGroupBox {
                border: 1px solid #313244;
                border-radius: 8px;
                margin-top: 12px;
                padding: 10px;
                font-weight: bold;
                color: #cdd6f4;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 5px 0 5px;
            }
            QTextEdit {
                background-color: #181825;
                border: 1px solid #313244;
                border-radius: 6px;
                color: #a6e3a1;
                font-family: 'Consolas', monospace;
            }
        """)

        # Core State
        self.server_thread = None
        self.discovery = None
        self.local_worker = None
        self.port = 8000
        
        # Video encoding logic state
        self.input_file = ""
        self.output_file = ""
        self.temp_dir = ""
        self.output_chunks_dir = ""
        
        self.total_duration = 0.0
        self.job_id = ""
        self.current_timeline_position = 0.0
        self.chunk_index = 0
        
        self.active_tasks: Dict[str, Dict[str, Any]] = {}
        self.retry_queue: List[Dict[str, Any]] = []
        self.completed_chunks: List[Dict[str, Any]] = []
        
        self.encoding_active = False
        self.start_time = 0.0
        self.audio_mode = "separate" # separate, combined
        self.audio_extraction_in_progress = False
        
        self.worker_widgets: Dict[str, WorkerWidget] = {}

        # Set up UI
        self.setup_ui()

        # Connect signals
        signals.worker_registered.connect(self.on_worker_registered)
        signals.worker_disconnected.connect(self.on_worker_disconnected)
        signals.task_progress.connect(self.on_task_progress)
        signals.task_completed.connect(self.on_task_completed)
        signals.log_message.connect(self.log)

        # Pre-populate server address
        self.ip_label.setText(f"Hub IP: {get_local_ip()} | Port: {self.port}")
        
        # Create default temporary paths
        cwd = os.getcwd()
        self.temp_dir = os.path.join(cwd, "workspace_temp")
        self.output_chunks_dir = os.path.join(cwd, "workspace_output_chunks")

    def setup_ui(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QHBoxLayout(central_widget)

        # Left Column: Configuration & Status
        left_layout = QVBoxLayout()
        
        # Server Status Group
        server_group = QGroupBox("Server Control")
        server_vbox = QVBoxLayout(server_group)
        self.ip_label = QLabel("Hub IP: Detecting...")
        self.ip_label.setStyleSheet("font-weight: bold; font-size: 13px; color: #f9e2af;")
        
        server_control_hbox = QHBoxLayout()
        self.start_server_btn = QPushButton("Start Hub Server")
        self.start_server_btn.clicked.connect(self.start_server)
        self.stop_server_btn = QPushButton("Stop Hub Server")
        self.stop_server_btn.setEnabled(False)
        self.stop_server_btn.setStyleSheet("background-color: #f38ba8; color: #11111b;")
        self.stop_server_btn.clicked.connect(self.stop_server)
        
        server_control_hbox.addWidget(self.start_server_btn)
        server_control_hbox.addWidget(self.stop_server_btn)
        
        server_vbox.addWidget(self.ip_label)
        server_vbox.addLayout(server_control_hbox)
        left_layout.addWidget(server_group)

        # File Configuration Group
        config_group = QGroupBox("Job Configuration")
        config_vbox = QVBoxLayout(config_group)
        
        # Input file
        input_hbox = QHBoxLayout()
        input_hbox.addWidget(QLabel("Input Video:"))
        self.input_edit = QLineEdit()
        self.input_edit.setPlaceholderText("Select input file...")
        self.input_browse = QPushButton("Browse")
        self.input_browse.clicked.connect(self.browse_input)
        input_hbox.addWidget(self.input_edit)
        input_hbox.addWidget(self.input_browse)
        config_vbox.addLayout(input_hbox)
        
        # Output file
        output_hbox = QHBoxLayout()
        output_hbox.addWidget(QLabel("Output Save:"))
        self.output_edit = QLineEdit()
        self.output_edit.setPlaceholderText("Select destination path...")
        self.output_browse = QPushButton("Browse")
        self.output_browse.clicked.connect(self.browse_output)
        output_hbox.addWidget(self.output_edit)
        output_hbox.addWidget(self.output_browse)
        config_vbox.addLayout(output_hbox)
        
        # FFMPEG Arguments
        args_layout = QHBoxLayout()
        args_layout.addWidget(QLabel("FFmpeg Arguments:"))
        self.args_edit = QLineEdit("-c:v libx264 -preset fast -crf 23")
        args_layout.addWidget(self.args_edit)
        config_vbox.addLayout(args_layout)
        
        # Audio configuration
        audio_layout = QHBoxLayout()
        self.audio_check = QCheckBox("Extract & process audio separately on PC")
        self.audio_check.setChecked(True)
        audio_layout.addWidget(self.audio_check)
        config_vbox.addLayout(audio_layout)
        
        # PC Worker participation
        pc_worker_layout = QHBoxLayout()
        self.pc_worker_check = QCheckBox("PC participates in encoding as worker")
        self.pc_worker_check.setChecked(True)
        pc_worker_layout.addWidget(self.pc_worker_check)
        config_vbox.addLayout(pc_worker_layout)
        
        # Action Buttons
        actions_hbox = QHBoxLayout()
        self.encode_btn = QPushButton("Start Encoding")
        self.encode_btn.setStyleSheet("background-color: #a6e3a1; color: #11111b; font-size: 14px;")
        self.encode_btn.clicked.connect(self.start_encoding_job)
        self.encode_btn.setEnabled(False)
        
        self.abort_btn = QPushButton("Abort Job")
        self.abort_btn.setStyleSheet("background-color: #f38ba8; color: #11111b;")
        self.abort_btn.setEnabled(False)
        self.abort_btn.clicked.connect(self.abort_job)
        
        self.reveal_btn = QPushButton("Show in Explorer")
        self.reveal_btn.setStyleSheet("background-color: #b4befe; color: #11111b;")
        self.reveal_btn.setEnabled(False)
        self.reveal_btn.clicked.connect(self.open_output_folder)
        
        actions_hbox.addWidget(self.encode_btn)
        actions_hbox.addWidget(self.abort_btn)
        actions_hbox.addWidget(self.reveal_btn)
        config_vbox.addLayout(actions_hbox)
        
        left_layout.addWidget(config_group)

        # Logging Group
        log_group = QGroupBox("Console Output")
        log_vbox = QVBoxLayout(log_group)
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        log_vbox.addWidget(self.log_text)
        left_layout.addWidget(log_group)

        main_layout.addLayout(left_layout, 1)

        # Right Column: Workers Monitor & Progress
        right_layout = QVBoxLayout()
        
        # Overall Progress Group
        progress_group = QGroupBox("Overall Job Progress")
        progress_vbox = QVBoxLayout(progress_group)
        self.overall_progress_bar = QProgressBar()
        self.overall_progress_bar.setRange(0, 100)
        self.overall_progress_bar.setValue(0)
        self.overall_progress_bar.setStyleSheet("""
            QProgressBar {
                background-color: #1e1e2e;
                border: 1px solid #313244;
                border-radius: 6px;
                height: 25px;
                text-align: center;
                color: #cdd6f4;
                font-weight: bold;
            }
            QProgressBar::chunk {
                background-color: #a6e3a1;
                border-radius: 5px;
            }
        """)
        progress_vbox.addWidget(self.overall_progress_bar)
        
        self.progress_details_label = QLabel("Status: Idle | Chunks: 0/0 | Time: --:--")
        self.progress_details_label.setStyleSheet("color: #a6adc8; font-size: 12px;")
        progress_vbox.addWidget(self.progress_details_label)
        
        right_layout.addWidget(progress_group)

        # Workers Group
        workers_group = QGroupBox("Connected Worker Spokes")
        workers_vbox = QVBoxLayout(workers_group)
        
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("border: none; background-color: transparent;")
        
        self.scroll_content = QWidget()
        self.workers_list_layout = QVBoxLayout(self.scroll_content)
        self.workers_list_layout.addStretch()  # Keep it aligned to the top
        scroll.setWidget(self.scroll_content)
        
        workers_vbox.addWidget(scroll)
        right_layout.addWidget(workers_group, 1)

        main_layout.addLayout(right_layout, 1)

    def log(self, msg: str):
        import threading
        if threading.current_thread() is not threading.main_thread():
            signals.log_message.emit(msg)
        else:
            logger.info(msg)
            self.log_text.append(f"[{time.strftime('%H:%M:%S')}] {msg}")
            # Scroll to bottom
            self.log_text.ensureCursorVisible()

    # --- Server Management ---
    def start_server(self):
        self.log("Starting server...")
        set_directories(self.temp_dir, self.output_chunks_dir)
        setup_server_hooks()
        
        self.server_thread = UvicornServerThread("0.0.0.0", self.port)
        self.server_thread.start()
        
        self.discovery = HubDiscovery(self.port)
        self.discovery.start()
        
        self.start_server_btn.setEnabled(False)
        self.stop_server_btn.setEnabled(True)
        self.encode_btn.setEnabled(True)
        self.log(f"Server started on port {self.port}. Discovery service active.")

    def stop_server(self):
        self.log("Stopping server and discovery...")
        
        if self.encoding_active:
            self.abort_job()
            
        if self.discovery:
            self.discovery.stop()
            self.discovery = None
            
        if self.server_thread:
            self.server_thread.stop()
            self.server_thread = None
            
        self.start_server_btn.setEnabled(True)
        self.stop_server_btn.setEnabled(False)
        self.encode_btn.setEnabled(False)
        self.log("Server and discovery stopped.")

    # --- File Dialog Browser ---
    def browse_input(self):
        f, _ = QFileDialog.getOpenFileName(self, "Select Input Video File", "", "Videos (*.mp4 *.mkv *.mov *.avi)")
        if f:
            self.input_edit.setText(f)
            self.input_file = f
            # Propose default output file path
            base, ext = os.path.splitext(f)
            self.output_edit.setText(f"{base}_encoded.mp4")
            self.output_file = f"{base}_encoded.mp4"

    def browse_output(self):
        f, _ = QFileDialog.getSaveFileName(self, "Select Encoded Video Destination", "", "Video Files (*.mp4)")
        if f:
            self.output_edit.setText(f)
            self.output_file = f

    # --- Workers State Slots ---
    @Slot(object)
    def on_worker_registered(self, worker: WorkerSession):
        self.log(f"New worker registered: {worker.name} ({worker.worker_id})")
        
        widget = WorkerWidget(worker)
        # Add to top of scroll layout
        self.workers_list_layout.insertWidget(self.workers_list_layout.count() - 1, widget)
        self.worker_widgets[worker.worker_id] = widget
        
        # If encoding is active, immediately assign a task
        if self.encoding_active:
            self.assign_next_task(worker.worker_id)

    @Slot(str)
    def on_worker_disconnected(self, worker_id: str):
        if worker_id in self.worker_widgets:
            widget = self.worker_widgets[worker_id]
            self.workers_list_layout.removeWidget(widget)
            widget.deleteLater()
            del self.worker_widgets[worker_id]
            self.log(f"Worker disconnected: {worker_id}")

    @Slot(str, str, float, float, int)
    def on_task_progress(self, worker_id: str, task_id: str, percent: float, speed: float, fps: int):
        if worker_id in self.worker_widgets:
            self.worker_widgets[worker_id].update_progress(task_id, percent, speed, fps)
            
        # Update active task state
        if task_id in self.active_tasks:
            self.active_tasks[task_id]["percent"] = percent
            
        # Trigger overall progress UI redraw
        self.update_overall_progress_ui()

    @Slot(str, str, float)
    def on_task_completed(self, worker_id: str, task_id: str, speed_multiplier: float):
        if worker_id in self.worker_widgets:
            self.worker_widgets[worker_id].set_idle()
            
        # If task failed (speed is 0.0) or connection dropped before finishing
        if speed_multiplier <= 0.0:
            if task_id in self.active_tasks:
                task_data = self.active_tasks.pop(task_id)
                self.log(f"Task {task_id} failed or was abandoned. Re-queuing.")
                self.retry_queue.append(task_data)
        else:
            # Succeeded
            if task_id in self.active_tasks:
                task_data = self.active_tasks.pop(task_id)
                task_data["encoded_file"] = f"{task_id}_encoded.mp4"
                self.completed_chunks.append(task_data)
                self.log(f"Chunk completed: {task_id} at {speed_multiplier:.2f}x speed.")
        
        # Check if job is finished or we need to assign next task
        if self.encoding_active:
            self.check_job_status_and_continue()

    # --- Encoding Flow ---
    def start_encoding_job(self):
        self.input_file = self.input_edit.text()
        self.output_file = self.output_edit.text()
        
        if not self.input_file or not os.path.exists(self.input_file):
            self.log("Error: Input file does not exist.")
            return
        if not self.output_file:
            self.log("Error: Output save location not specified.")
            return
            
        # Clean directories
        for d in [self.temp_dir, self.output_chunks_dir]:
            if os.path.exists(d):
                shutil.rmtree(d)
            os.makedirs(d, exist_ok=True)
            
        # Disable buttons and inputs immediately to keep GUI clean
        self.input_browse.setEnabled(False)
        self.output_browse.setEnabled(False)
        self.encode_btn.setEnabled(False)
        self.abort_btn.setEnabled(True)
        self.reveal_btn.setEnabled(False)
        
        self.encoding_active = True
        self.start_time = time.time()
        self.job_id = str(uuid.uuid4())
        server.state.source_video_path = self.input_file
        self.current_timeline_position = 0.0
        self.chunk_index = 0
        self.active_tasks.clear()
        self.retry_queue.clear()
        self.completed_chunks.clear()
        
        self.audio_mode = "separate" if self.audio_check.isChecked() else "combined"
        self.progress_details_label.setText("Status: Preparing encoding job...")
        
        def run_preparation():
            try:
                self.log(f"Analyzing source video in background: {self.input_file}")
                self.total_duration = get_ffmpeg_duration(self.input_file)
                self.log(f"Video duration: {self.total_duration:.2f} seconds.")
                
                # Start worker assignment loops immediately (spokes can download full video)
                QMetaObject.invokeMethod(self, "on_preparation_complete", Qt.QueuedConnection)
                
                # Concurrently extract audio in the background if enabled
                if self.audio_mode == "separate":
                    self.log("Extracting audio from input video concurrently in background...")
                    self.extract_audio()
                    
            except Exception as e:
                self.log(f"Critical error during preparation: {e}")
                QMetaObject.invokeMethod(self, "on_preparation_failed", Qt.QueuedConnection)
                
        import threading
        threading.Thread(target=run_preparation, daemon=True).start()

    @Slot()
    def on_preparation_complete(self):
        if not self.encoding_active:
            return
        
        self.log("Preparation complete. Starting worker assigning loops...")
        
        # 1. Spawn Local PC Worker if enabled
        if self.pc_worker_check.isChecked():
            self.local_worker = LocalPCWorker(self.port, self.temp_dir)
            self.local_worker.start()
            
        # 2. Assign initial tasks to all available idle workers
        # Sleep slightly to allow local worker WebSocket handshake to complete
        QThread.msleep(500)
        
        for w_id, worker in list(state.workers.items()):
            if worker.status == "idle":
                self.assign_next_task(w_id)
                
        self.update_overall_progress_ui()

    @Slot()
    def on_preparation_failed(self):
        self.log("Error: Job preparation failed.")
        self.clean_up_job()
        self.encoding_active = False
        self.overall_progress_bar.setValue(0)
        self.progress_details_label.setText("Status: Failed")

    def extract_audio(self):
        """Extract and compress audio to a standalone file on the PC."""
        self.audio_extraction_in_progress = True
        audio_out = os.path.join(self.temp_dir, "audio_only.aac")
        cmd = ["ffmpeg", "-y", "-i", self.input_file, "-vn", "-c:a", "aac", "-b:a", "192k", audio_out]
        try:
            subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.log("Audio track extracted successfully.")
        except Exception as e:
            self.log(f"Warning: Audio extraction failed (video may have no audio): {e}")
        finally:
            self.audio_extraction_in_progress = False

    def assign_next_task(self, worker_id: str):
        if not self.encoding_active:
            return
            
        worker = state.workers.get(worker_id)
        if not worker or worker.status != "idle":
            return
            
        # 1. Check retry queue first
        if self.retry_queue:
            task_data = self.retry_queue.pop(0)
            self._send_task_to_worker(worker, task_data)
            return
            
        # 2. Check if there is timeline left to slice
        if self.current_timeline_position >= self.total_duration:
            return # Timeline completed, no new tasks
            
        # Calculate dynamic duration
        avg_speed = worker.get_avg_speed()
        target_duration = max(3.0, min(30.0, avg_speed * 5.0))
        
        start_time = self.current_timeline_position
        end_time = start_time + target_duration
        
        # If the segment is too short or we're near the end, snap to video duration
        if (self.total_duration - end_time) < 3.0 or end_time >= self.total_duration:
            end_time = self.total_duration
            
        duration = end_time - start_time
        if duration <= 0.1:
            return
            
        # Task identifiers
        task_id = f"chunk_{self.chunk_index:05d}"
        self.chunk_index += 1
        
        task_data = {
            "task_id": task_id,
            "start": start_time,
            "duration": duration,
            "percent": 0.0
        }
        
        self.current_timeline_position = end_time
        self._send_task_to_worker(worker, task_data)

    def _send_task_to_worker(self, worker: WorkerSession, task_data: Dict[str, Any]):
        task_id = task_data["task_id"]
        worker.status = "busy"
        worker.active_task_id = task_id
        
        # Store in active tasks
        self.active_tasks[task_id] = task_data
        
        # Construct ffmpeg args command template
        ffmpeg_args = self.args_edit.text()
        if self.audio_mode == "separate" and "-an" not in ffmpeg_args:
            ffmpeg_args += " -an"
            
        # Send WebSocket assign message
        local_ip = get_local_ip()
        video_url = f"http://{local_ip}:{self.port}/download/source"
        
        payload = {
            "type": "task_assign",
            "task_id": task_id,
            "job_id": self.job_id,
            "video_url": video_url,
            "local_video_path": self.input_file,
            "start_time": task_data["start"],
            "duration": task_data["duration"],
            "ffmpeg_args": ffmpeg_args
        }
        
        asyncio.run_coroutine_threadsafe(
            worker.websocket.send_json(payload),
            state.loop
        )
        self.log(f"Assigned chunk {task_id} ({task_data['duration']:.2f}s) to worker {worker.name}")

    def check_job_status_and_continue(self):
        # 1. First, check if there are idle workers who need jobs
        for w_id, worker in list(state.workers.items()):
            if worker.status == "idle":
                self.assign_next_task(w_id)
                
        self.update_overall_progress_ui()
        
        # 2. Check if job is entirely done
        if (self.current_timeline_position >= self.total_duration and 
            not self.active_tasks and 
            not self.retry_queue):
            # All done!
            self.log("All chunks completed successfully! Beginning final merge...")
            self.merge_outputs()

    def update_overall_progress_ui(self):
        if not self.encoding_active or self.total_duration <= 0.0:
            return
            
        # Total duration encoded is sum of completed chunks durations + current progress of active chunks
        encoded_duration = sum(t["duration"] for t in self.completed_chunks)
        for task_id, task in self.active_tasks.items():
            encoded_duration += task["duration"] * (task["percent"] / 100.0)
            
        percent = min(100.0, (encoded_duration / self.total_duration) * 100.0)
        self.overall_progress_bar.setValue(int(percent))
        
        elapsed = time.time() - self.start_time
        time_str = time.strftime('%M:%S', time.gmtime(elapsed))
        
        details = (
            f"Encoding... | Progress: {percent:.1f}% | "
            f"Chunks completed: {len(self.completed_chunks)} | "
            f"Elapsed: {time_str}"
        )
        self.progress_details_label.setText(details)

    def merge_outputs(self):
        self.encoding_active = False
        self.progress_details_label.setText("Status: Merging output chunks...")
        
        # Sort completed chunks by start time to guarantee order
        self.completed_chunks.sort(key=lambda x: x["start"])
        
        def run_merge():
            success = False
            try:
                # Wait for audio extraction if it is still running
                if self.audio_mode == "separate" and self.audio_extraction_in_progress:
                    self.log("Waiting for background audio extraction to complete before merging...")
                    while self.audio_extraction_in_progress:
                        time.sleep(0.5)
                        
                concat_txt_path = os.path.join(self.temp_dir, "concat.txt")
                with open(concat_txt_path, "w") as f:
                    for chunk in self.completed_chunks:
                        # Concat list requires file paths relative or absolute
                        encoded_file_path = os.path.join(self.output_chunks_dir, chunk["encoded_file"])
                        # Escape single quotes in file paths for ffmpeg concat
                        escaped_path = encoded_file_path.replace("'", "'\\''")
                        f.write(f"file '{escaped_path}'\n")
                        
                self.log(f"Concat list written to {concat_txt_path}")
                
                # If Separate Audio mode
                if self.audio_mode == "separate" and os.path.exists(os.path.join(self.temp_dir, "audio_only.aac")):
                    temp_video_only = os.path.join(self.temp_dir, "temp_video_only.mp4")
                    
                    # 1. Merge video chunks
                    self.log("Concatenating video chunks (no-transcode demux)...")
                    cmd_concat = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_txt_path, "-c", "copy", temp_video_only]
                    subprocess.run(cmd_concat, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    
                    # 2. Mux with original audio
                    self.log("Muxing video with original audio track...")
                    audio_file = os.path.join(self.temp_dir, "audio_only.aac")
                    cmd_mux = [
                        "ffmpeg", "-y", "-i", temp_video_only, "-i", audio_file,
                        "-c:v", "copy", "-c:a", "copy", "-map", "0:v:0", "-map", "1:a:0",
                        self.output_file
                    ]
                    subprocess.run(cmd_mux, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                else:
                    # Combined Mode
                    self.log("Concatenating chunks directly...")
                    cmd_concat = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_txt_path, "-c", "copy", self.output_file]
                    subprocess.run(cmd_concat, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    
                success = True
            except Exception as e:
                self.log(f"Critical error during merging: {e}")
            finally:
                if success:
                    QMetaObject.invokeMethod(self, "on_merge_complete", Qt.QueuedConnection)
                else:
                    QMetaObject.invokeMethod(self, "on_merge_failed", Qt.QueuedConnection)
                    
        import threading
        threading.Thread(target=run_merge, daemon=True).start()

    @Slot()
    def on_merge_complete(self):
        elapsed = time.time() - self.start_time
        self.log(f"Job completed successfully in {elapsed:.1f}s!")
        self.overall_progress_bar.setValue(100)
        self.progress_details_label.setText(f"Status: Finished | Elapsed: {time.strftime('%M:%S', time.gmtime(elapsed))}")
        self.clean_up_job()
        self.reveal_btn.setEnabled(True)

    @Slot()
    def on_merge_failed(self):
        self.log("Job failed during merging phase.")
        self.clean_up_job()
        self.overall_progress_bar.setValue(0)
        self.progress_details_label.setText("Status: Failed")

    def abort_job(self):
        if not self.encoding_active:
            return
        self.log("Aborting current encoding job...")
        self.encoding_active = False
        
        # Broadcast abort signal to all websocket clients
        for w_id, worker in list(state.workers.items()):
            if worker.websocket:
                try:
                    asyncio.run_coroutine_threadsafe(
                        worker.websocket.send_json({"type": "abort"}),
                        state.loop
                    )
                except Exception:
                    pass
                    
        self.clean_up_job()
        self.overall_progress_bar.setValue(0)
        self.progress_details_label.setText("Status: Aborted")

    def clean_up_job(self):
        # Stop local worker if running
        if self.local_worker:
            self.local_worker.stop()
            self.local_worker = None
            
        # Enable config buttons
        self.input_browse.setEnabled(True)
        self.output_browse.setEnabled(True)
        self.encode_btn.setEnabled(True)
        self.abort_btn.setEnabled(False)
        
        # Reset worker status badge widgets
        for w_id, widget in self.worker_widgets.items():
            widget.set_idle()
            
        # Remove temporary chunks
        for d in [self.temp_dir, self.output_chunks_dir]:
            if os.path.exists(d):
                try:
                    shutil.rmtree(d)
                except Exception as e:
                    logger.error(f"Failed to delete temp dir {d}: {e}")

    def open_output_folder(self):
        output_path = self.output_edit.text()
        if output_path and os.path.exists(output_path):
            norm_path = os.path.normpath(output_path)
            subprocess.run(["explorer", f"/select,{norm_path}"])

    def closeEvent(self, event):
        self.stop_server()
        event.accept()

if __name__ == "__main__":
    # Create the application
    app_qt = QApplication(sys.argv)
    
    # Set default palette colors
    palette = app_qt.palette()
    palette.setColor(QPalette.PlaceholderText, QColor("#7f849c"))
    app_qt.setPalette(palette)
    
    # Launch main window
    window = MainWindow()
    window.show()
    
    sys.exit(app_qt.exec())
