import os
import uuid
import logging
import asyncio
from typing import Dict, Any, Callable
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, Form, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

logger = logging.getLogger(__name__)

class WorkerSession:
    def __init__(self, worker_id: str, name: str, is_local: bool, websocket: WebSocket = None):
        self.worker_id = worker_id
        self.name = name
        self.is_local = is_local
        self.websocket = websocket
        self.active_task_id = None
        self.speed_history = []
        self.last_reported_speed = 1.0
        self.status = "idle"  # idle, busy, offline

    def get_avg_speed(self) -> float:
        if not self.speed_history:
            return 1.0
        # Simple moving average of the last 3 chunks
        return sum(self.speed_history[-3:]) / len(self.speed_history[-3:])

class ServerState:
    def __init__(self):
        self.workers: Dict[str, WorkerSession] = {}
        self.tasks: Dict[str, Dict[str, Any]] = {}
        self.temp_chunks_dir = ""
        self.output_chunks_dir = ""
        self.source_video_path = ""
        self.loop = None  # Capture FastAPI thread's event loop
        
        # Callback hooks for UI communication (set by main app)
        self.on_worker_registered: Callable[[WorkerSession], None] = None
        self.on_worker_disconnected: Callable[[str], None] = None
        self.on_task_progress: Callable[[str, str, float, float, int], None] = None  # worker_id, task_id, percent, speed, fps
        self.on_task_completed: Callable[[str, str, float], None] = None  # worker_id, task_id, speed

app = FastAPI()
state = ServerState()

@app.on_event("startup")
async def startup_event():
    state.loop = asyncio.get_running_loop()
    logger.info("FastAPI server event loop captured.")

# Enable CORS for convenience
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def set_directories(temp_dir: str, output_dir: str):
    """Dynamically set chunk directories and mount static files."""
    state.temp_chunks_dir = temp_dir
    state.output_chunks_dir = output_dir
    
    # Mount the static files path for chunk downloads
    os.makedirs(temp_dir, exist_ok=True)
    os.makedirs(output_dir, exist_ok=True)
    
    # Remove existing download mount if present to avoid runtime errors
    for route in list(app.routes):
        if route.path == "/download":
            app.routes.remove(route)
            
    app.mount("/download", StaticFiles(directory=temp_dir), name="download")
    logger.info(f"Mounted static file download directory: {temp_dir}")

@app.get("/health")
def health():
    return {"status": "ok", "workers": len(state.workers)}

@app.get("/download/source")
async def download_source():
    if not state.source_video_path or not os.path.exists(state.source_video_path):
        raise HTTPException(status_code=404, detail="Source video not found or not initialized.")
    return FileResponse(state.source_video_path, filename=os.path.basename(state.source_video_path))

@app.post("/upload/{task_id}")
async def upload_chunk(
    task_id: str,
    file: UploadFile = File(...),
    speed_multiplier: float = Form(...),
    worker_id: str = Form(...)
):
    if not state.output_chunks_dir:
        raise HTTPException(status_code=500, detail="Server chunk directories not initialized.")
        
    logger.info(f"Receiving completed chunk {task_id} from worker {worker_id} (speed: {speed_multiplier}x)")
    
    # Save the uploaded file
    file_path = os.path.join(state.output_chunks_dir, f"{task_id}_encoded.mp4")
    try:
        with open(file_path, "wb") as f:
            f.write(await file.read())
    except Exception as e:
        logger.error(f"Failed to write uploaded chunk {task_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to write file: {str(e)}")

    # Update state
    if worker_id in state.workers:
        worker = state.workers[worker_id]
        worker.active_task_id = None
        worker.status = "idle"
        worker.last_reported_speed = speed_multiplier
        worker.speed_history.append(speed_multiplier)
    
    # Trigger task completion callback
    if state.on_task_completed:
        state.on_task_completed(worker_id, task_id, speed_multiplier)

    return {"status": "success"}

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    worker_id = str(uuid.uuid4())
    registered_worker = None
    
    try:
        # Handshake/Registration
        data = await websocket.receive_json()
        if data.get("type") == "register":
            name = data.get("name", "Unknown Spoke")
            is_local = data.get("is_local_pc", False)
            
            registered_worker = WorkerSession(
                worker_id=worker_id,
                name=name,
                is_local=is_local,
                websocket=websocket
            )
            state.workers[worker_id] = registered_worker
            
            logger.info(f"Worker {name} ({worker_id}) registered successfully.")
            await websocket.send_json({
                "type": "registered",
                "worker_id": worker_id
            })
            
            if state.on_worker_registered:
                state.on_worker_registered(registered_worker)
        else:
            logger.warning("Worker connection failed handshake. Closing.")
            await websocket.close(code=1008)
            return

        # Main communication loop
        while True:
            msg = await websocket.receive_json()
            msg_type = msg.get("type")
            
            if msg_type == "progress_update":
                task_id = msg.get("task_id")
                percent = msg.get("percent", 0.0)
                speed = msg.get("speed", 1.0)
                fps = msg.get("fps", 0)
                
                # Keep active status updated
                registered_worker.status = "busy"
                registered_worker.active_task_id = task_id
                
                if state.on_task_progress:
                    state.on_task_progress(worker_id, task_id, percent, speed, fps)
                    
            elif msg_type == "error":
                task_id = msg.get("task_id")
                reason = msg.get("reason", "Unknown error")
                logger.error(f"Worker {registered_worker.name} reported error on task {task_id}: {reason}")
                
                registered_worker.status = "idle"
                registered_worker.active_task_id = None
                
                # Re-queue/fail task through completed hook with 0.0 speed
                if state.on_task_completed:
                    state.on_task_completed(worker_id, task_id, 0.0)
                    
    except WebSocketDisconnect:
        logger.info(f"Worker disconnected: {worker_id}")
    except Exception as e:
        logger.error(f"Error in websocket loop for {worker_id}: {e}")
    finally:
        if worker_id in state.workers:
            # Re-queue task if it was busy
            active_task = state.workers[worker_id].active_task_id
            if active_task and state.on_task_completed:
                logger.info(f"Re-queuing abandoned task {active_task} from disconnected worker.")
                state.on_task_completed(worker_id, active_task, 0.0)
            
            del state.workers[worker_id]
            
            if state.on_worker_disconnected:
                state.on_worker_disconnected(worker_id)
