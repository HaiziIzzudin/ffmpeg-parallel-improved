import os
import re
import sys
import time
import shutil
import logging
import asyncio
import subprocess
import requests
import json
import threading
import socket
import websockets

logger = logging.getLogger(__name__)

def get_ffmpeg_duration(file_path):
    """Run ffprobe to get the duration of the video file in seconds."""
    cmd = [
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", file_path
    ]
    try:
        output = subprocess.check_output(cmd, stderr=subprocess.STDOUT, universal_newlines=True)
        return float(output.strip())
    except Exception as e:
        logger.error(f"Error checking duration with ffprobe: {e}")
        return 1.0

class LocalPCWorker:
    def __init__(self, server_port: int, workspace_dir: str):
        self.server_port = server_port
        self.workspace_dir = workspace_dir
        self.temp_dir = os.path.join(workspace_dir, "local_worker_temp")
        os.makedirs(self.temp_dir, exist_ok=True)
        self.loop = None
        self.thread = None
        self.main_task = None
        self.running = False
        self.current_process = None

    def start(self):
        self.running = True
        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.thread.start()
        logger.info("Local PC Worker thread started.")

    def stop(self):
        self.running = False
        if self.current_process:
            try:
                self.current_process.kill()
                logger.info("Killed active local FFmpeg process.")
            except Exception:
                pass
        if self.loop and self.main_task:
            self.loop.call_soon_threadsafe(self.main_task.cancel)
        if os.path.exists(self.temp_dir):
            try:
                shutil.rmtree(self.temp_dir)
            except Exception:
                pass
        logger.info("Local PC Worker stopped.")

    def _run_loop(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.main_task = self.loop.create_task(self._worker_main())
        try:
            self.loop.run_until_complete(self.main_task)
        except asyncio.CancelledError:
            logger.info("Local Worker main task cancelled.")
        except Exception as e:
            logger.error(f"Local Worker thread run error: {e}")
        finally:
            self.loop.close()

    async def _worker_main(self):
        uri = f"ws://127.0.0.1:{self.server_port}/ws"
        while self.running:
            try:
                logger.info(f"Local Worker connecting to {uri}...")
                async with websockets.connect(uri) as websocket:
                    # Handshake
                    hostname = socket.gethostname()
                    await websocket.send(json.dumps({
                        "type": "register",
                        "name": f"Local PC ({hostname})",
                        "is_local_pc": True
                    }))
                    
                    response = await websocket.recv()
                    res_data = json.loads(response)
                    if res_data.get("type") != "registered":
                        logger.error("Local worker registration failed. Retrying.")
                        await asyncio.sleep(5)
                        continue
                    
                    worker_id = res_data.get("worker_id")
                    logger.info(f"Local Worker registered with ID: {worker_id}")
                    
                    # Connection successful, listen for tasks
                    while self.running:
                        msg_str = await websocket.recv()
                        msg = json.loads(msg_str)
                        
                        if msg.get("type") == "task_assign":
                            task_id = msg.get("task_id")
                            job_id = msg.get("job_id")
                            video_url = msg.get("video_url")
                            local_video_path = msg.get("local_video_path")
                            start_time = msg.get("start_time")
                            duration = msg.get("duration")
                            ffmpeg_args = msg.get("ffmpeg_args")
                            
                            # Start processing in a non-blocking thread pool task
                            asyncio.create_task(self._process_task(
                                websocket, worker_id, task_id, job_id, video_url,
                                local_video_path, start_time, duration, ffmpeg_args
                            ))
                        elif msg.get("type") == "abort":
                            logger.info("Received abort signal from server.")
                            if self.current_process:
                                self.current_process.kill()
                                
            except (websockets.exceptions.ConnectionClosed, ConnectionRefusedError):
                logger.warning("Local Worker connection lost. Reconnecting in 5 seconds...")
                await asyncio.sleep(5)
            except Exception as e:
                logger.error(f"Error in Local Worker loop: {e}")
                await asyncio.sleep(5)

    async def _process_task(self, websocket, worker_id, task_id, job_id, video_url, local_video_path, start_time, duration, ffmpeg_args):
        logger.info(f"Local Worker processing task: {task_id}")
        
        # 1. Determine input file path
        if local_video_path and os.path.exists(local_video_path):
            local_input = local_video_path
        else:
            local_input = os.path.join(self.temp_dir, f"{job_id}_source.mp4")
            if not os.path.exists(local_input):
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, self._download_file, video_url, local_input)
                
        local_output = os.path.join(self.temp_dir, f"{task_id}_out.mp4")
        if os.path.exists(local_output):
            try:
                os.remove(local_output)
            except Exception:
                pass
        
        try:
            loop = asyncio.get_event_loop()
            
            # 2. Assemble ffmpeg command
            # ffmpeg -y -ss [start_time] -i [input] -t [duration] [args] -progress pipe:1 [output]
            cmd = [
                "ffmpeg", "-y",
                "-ss", f"{start_time:.3f}",
                "-i", local_input,
                "-t", f"{duration:.3f}"
            ]
            cmd.extend(ffmpeg_args.split())
            cmd.extend(["-progress", "pipe:1", local_output])
            
            logger.info(f"Local Worker running command: {' '.join(cmd)}")
            
            # 4. Start FFmpeg process
            self.current_process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                universal_newlines=True
            )
            
            # Read progress from stdout
            last_progress_time = time.time()
            speed = 1.0
            fps = 0
            
            while True:
                line = await loop.run_in_executor(None, self.current_process.stdout.readline)
                if not line:
                    break
                
                parts = line.strip().split("=")
                if len(parts) == 2:
                    key, val = parts[0], parts[1]
                    if key == "out_time_us":
                        # Progress time in microseconds
                        try:
                            time_us = float(val)
                            percent = min(100.0, (time_us / 1000000.0 / duration) * 100.0)
                            
                            # Throttle updates to websocket (every 0.5s)
                            if time.time() - last_progress_time > 0.5:
                                await websocket.send(json.dumps({
                                    "type": "progress_update",
                                    "task_id": task_id,
                                    "percent": percent,
                                    "speed": speed,
                                    "fps": fps
                                }))
                                last_progress_time = time.time()
                        except ValueError:
                            pass
                    elif key == "speed":
                        # speed=  2.5x -> parse 2.5
                        val_cleaned = val.replace("x", "").strip()
                        try:
                            speed = float(val_cleaned)
                        except ValueError:
                            pass
                    elif key == "fps":
                        try:
                            fps = int(float(val.strip()))
                        except ValueError:
                            pass
            
            # Wait for exit
            self.current_process.wait()
            exit_code = self.current_process.returncode
            self.current_process = None
            
            if exit_code != 0:
                raise Exception(f"FFmpeg exited with non-zero code: {exit_code}")
                
            # Send 100% progress
            await websocket.send(json.dumps({
                "type": "progress_update",
                "task_id": task_id,
                "percent": 100.0,
                "speed": speed,
                "fps": fps
            }))
            
            # 5. Upload finished file
            upload_url = f"http://127.0.0.1:{self.server_port}/upload/{task_id}"
            await loop.run_in_executor(None, self._upload_file, upload_url, local_output, speed, worker_id)
            logger.info(f"Local Worker finished task: {task_id} successfully.")
            
        except Exception as e:
            logger.error(f"Local Worker error processing task {task_id}: {e}")
            try:
                await websocket.send(json.dumps({
                    "type": "error",
                    "task_id": task_id,
                    "reason": str(e)
                }))
            except Exception:
                pass
        finally:
            # Clean up temp files for this task
            if os.path.exists(local_output):
                try:
                    os.remove(local_output)
                except Exception:
                    pass

    def _download_file(self, url, dest_path):
        r = requests.get(url, stream=True)
        r.raise_for_status()
        with open(dest_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)

    def _upload_file(self, url, file_path, speed, worker_id):
        with open(file_path, "rb") as f:
            files = {"file": f}
            data = {
                "speed_multiplier": speed,
                "worker_id": worker_id
            }
            r = requests.post(url, files=files, data=data)
            r.raise_for_status()
