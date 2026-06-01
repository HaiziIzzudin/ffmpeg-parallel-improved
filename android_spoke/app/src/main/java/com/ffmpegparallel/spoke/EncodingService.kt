package com.ffmpegparallel.spoke

import android.app.*
import android.content.Context
import android.content.Intent
import android.media.MediaMetadataRetriever
import android.os.Binder
import android.os.Build
import android.os.IBinder
import android.util.Log
import androidx.core.app.NotificationCompat
import com.arthenica.ffmpegkit.FFmpegKit
import com.arthenica.ffmpegkit.FFmpegKitConfig
import okhttp3.*
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.RequestBody.Companion.asRequestBody
import org.json.JSONObject
import java.io.File
import java.io.FileOutputStream
import java.io.IOException
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit

class EncodingService : Service() {
    private val TAG = "EncodingService"
    private val CHANNEL_ID = "EncodingServiceChannel"
    private val NOTIFICATION_ID = 1

    private val binder = LocalBinder()
    private val executor: ExecutorService = Executors.newSingleThreadExecutor()
    private val okHttpClient = OkHttpClient.Builder()
        .connectTimeout(10, TimeUnit.SECONDS)
        .readTimeout(0, TimeUnit.MILLISECONDS) // Unlimited for WS
        .writeTimeout(0, TimeUnit.MILLISECONDS)
        .build()

    private var webSocket: WebSocket? = null
    var isConnected = false
    private var currentSessionId: String? = null
    private var activeTaskId: String? = null
    private var activeSession: com.arthenica.ffmpegkit.FFmpegSession? = null
    private var cachedJobId: String? = null
    private var cachedFile: File? = null

    // UI Callback Listener
    interface StatusListener {
        fun onStatusChanged(status: String)
        fun onLogAdded(log: String)
    }
    
    private var statusListener: StatusListener? = null
    private var lastStatus = "Initializing..."

    inner class LocalBinder : Binder() {
        fun getService(): EncodingService = this@EncodingService
    }

    override fun onCreate() {
        super.onCreate()
        createNotificationChannel()
        startForeground(NOTIFICATION_ID, buildNotification("Searching for PC Hub..."))
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        return START_NOT_STICKY
    }

    override fun onBind(intent: Intent?): IBinder {
        return binder
    }

    fun setStatusListener(listener: StatusListener?) {
        this.statusListener = listener
        listener?.onStatusChanged(lastStatus)
    }

    private fun updateStatus(status: String) {
        lastStatus = status
        Log.i(TAG, status)
        statusListener?.onStatusChanged(status)
        updateNotification(status)
    }

    private fun addLog(log: String) {
        Log.d(TAG, log)
        statusListener?.onLogAdded(log)
    }

    fun connectToHub(ip: String, port: Int) {
        executor.submit {
            disconnect()
            
            val wsUrl = "ws://$ip:$port/ws"
            updateStatus("Connecting to WebSocket: $wsUrl")
            
            val request = Request.Builder().url(wsUrl).build()
            webSocket = okHttpClient.newWebSocket(request, object : WebSocketListener() {
                override fun onOpen(webSocket: WebSocket, response: Response) {
                    isConnected = true
                    val registerMsg = JSONObject().apply {
                        put("type", "register")
                        put("name", "${Build.MANUFACTURER} ${Build.MODEL}")
                        put("is_local_pc", false)
                    }
                    webSocket.send(registerMsg.toString())
                    updateStatus("Connected to Hub, registering...")
                }

                override fun onMessage(webSocket: WebSocket, text: String) {
                    try {
                        val msg = JSONObject(text)
                        when (msg.optString("type")) {
                            "registered" -> {
                                currentSessionId = msg.getString("worker_id")
                                updateStatus("Registered as $currentSessionId. Ready for tasks.")
                            }
                            "task_assign" -> {
                                val taskId = msg.getString("task_id")
                                val jobId = msg.getString("job_id")
                                val videoUrl = msg.getString("video_url")
                                val startTime = msg.getDouble("start_time")
                                val duration = msg.getDouble("duration")
                                val ffmpegArgs = msg.getString("ffmpeg_args")
                                
                                executor.submit {
                                    processTask(taskId, jobId, videoUrl, startTime, duration, ffmpegArgs)
                                }
                            }
                            "abort" -> {
                                addLog("Received abort signal from PC Hub.")
                                activeSession?.cancel()
                            }
                        }
                    } catch (e: Exception) {
                        addLog("Error parsing message: ${e.message}")
                    }
                }

                override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                    updateStatus("WebSocket closing: $reason")
                }

                override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                    isConnected = false
                    updateStatus("Disconnected from PC Hub.")
                }

                override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                    isConnected = false
                    updateStatus("Connection failure: ${t.message}")
                }
            })
        }
    }

    fun disconnect() {
        webSocket?.close(1000, "User disconnected")
        webSocket = null
        isConnected = false
        currentSessionId = null
        activeSession?.cancel()
        activeSession = null
        activeTaskId = null
        
        cachedFile?.let {
            if (it.exists()) {
                it.delete()
            }
        }
        cachedFile = null
        cachedJobId = null
        
        updateStatus("Disconnected.")
    }

    private fun processTask(
        taskId: String,
        jobId: String,
        videoUrl: String,
        startTime: Double,
        duration: Double,
        ffmpegArgs: String
    ) {
        activeTaskId = taskId
        addLog("Starting task: $taskId for job $jobId")

        val cacheDir = externalCacheDir ?: cacheDir
        val sourceFile = File(cacheDir, "job_${jobId}_source.mp4")
        val outputFile = File(cacheDir, "${taskId}_out.mp4")

        if (outputFile.exists()) outputFile.delete()

        try {
            // 1. Manage cache of source video file
            if (cachedJobId != jobId || !sourceFile.exists()) {
                // Invalidate old cache
                cachedFile?.let {
                    if (it.exists()) {
                        it.delete()
                        addLog("Deleted old cached source file: ${it.name}")
                    }
                }
                
                updateStatus("Downloading source video...")
                addLog("Downloading source video from $videoUrl")
                
                val request = Request.Builder().url(videoUrl).build()
                val response = okHttpClient.newCall(request).execute()
                if (!response.isSuccessful) {
                    throw IOException("Download of source video failed with code: ${response.code}")
                }

                response.body?.byteStream()?.use { input ->
                    FileOutputStream(sourceFile).use { output ->
                        input.copyTo(output)
                    }
                }
                
                cachedJobId = jobId
                cachedFile = sourceFile
                addLog("Downloaded source video successfully: ${sourceFile.name}")
            } else {
                addLog("Using cached source video: ${sourceFile.name}")
            }

            val durationMs = (duration * 1000).toLong()
            addLog("Segment duration: ${duration}s (start: ${startTime}s)")

            // 2. Assemble and execute FFmpeg Command
            // Command: -y -ss [startTime] -i [sourceFile] -t [duration] [ffmpegArgs] [outputFile]
            val cmdArgs = mutableListOf<String>().apply {
                add("-y")
                add("-ss")
                add(String.format(java.util.Locale.US, "%.3f", startTime))
                add("-i")
                add(sourceFile.absolutePath)
                add("-t")
                add(String.format(java.util.Locale.US, "%.3f", duration))
                addAll(ffmpeg_args_to_list(ffmpegArgs))
                add(outputFile.absolutePath)
            }
            val command = cmdArgs.joinToString(" ")
            addLog("Running FFmpeg: $command")
            updateStatus("Encoding segment $taskId...")

            // Intercept stats callback
            FFmpegKitConfig.enableStatisticsCallback { stats ->
                val timeMs = stats.time
                val percent = if (durationMs > 0) ((timeMs.toDouble() / durationMs) * 100.0).coerceAtMost(100.0) else 0.0
                
                // Send progress update via websocket
                val progressMsg = JSONObject().apply {
                    put("type", "progress_update")
                    put("task_id", taskId)
                    put("percent", percent)
                    put("speed", stats.speed)
                    put("fps", stats.videoFps)
                }
                webSocket?.send(progressMsg.toString())
            }

            // Sync execute so we remain in the executor thread sequential flow
            val session = FFmpegKit.execute(command)
            activeSession = session
            val returnCode = session.returnCode

            // Remove callbacks
            FFmpegKitConfig.enableStatisticsCallback(null)
            activeSession = null

            if (returnCode.isValueSuccess) {
                addLog("Encoding $taskId completed successfully.")
                updateStatus("Uploading chunk $taskId...")

                // 3. Upload file
                val uploadUrl = videoUrl.replace("/download/source", "/upload/$taskId")
                val requestBody = MultipartBody.Builder()
                    .setType(MultipartBody.FORM)
                    .addFormDataPart(
                        "file",
                        "${taskId}_encoded.mp4",
                        outputFile.asRequestBody("video/mp4".toMediaType())
                    )
                    .addFormDataPart("speed_multiplier", session.statistics.lastOrNull()?.speed?.toString() ?: "1.0")
                    .addFormDataPart("worker_id", currentSessionId ?: "")
                    .build()

                val uploadRequest = Request.Builder()
                    .url(uploadUrl)
                    .post(requestBody)
                    .build()

                val uploadResponse = okHttpClient.newCall(uploadRequest).execute()
                if (uploadResponse.isSuccessful) {
                    addLog("Uploaded chunk $taskId successfully.")
                    updateStatus("Idle. Ready for next task.")
                } else {
                    throw IOException("Upload failed: ${uploadResponse.code} - ${uploadResponse.message}")
                }
            } else if (returnCode.isValueCancel) {
                addLog("Task $taskId was canceled.")
                updateStatus("Idle. Ready for next task.")
            } else {
                throw Exception("FFmpeg failed with code $returnCode, logs: ${session.allLogsAsString}")
            }

        } catch (e: Exception) {
            addLog("Error in task $taskId: ${e.message}")
            try {
                val errorMsg = JSONObject().apply {
                    put("type", "error")
                    put("task_id", taskId)
                    put("reason", e.message ?: "Unknown error")
                }
                webSocket?.send(errorMsg.toString())
            } catch (ex: Exception) { /* ignored */ }
            updateStatus("Idle (Task failed).")
        } finally {
            activeTaskId = null
            // Delete temp output file
            if (outputFile.exists()) outputFile.delete()
        }
    }

    private fun ffmpeg_args_to_list(args: String): List<String> {
        val result = mutableListOf<String>()
        val matches = Regex("[^\\s\"']+|\"([^\"]*)\"|'([^']*)'").findAll(args)
        for (m in matches) {
            val s = m.value
            if (s.startsWith("\"") && s.endsWith("\"")) {
                result.add(s.substring(1, s.length - 1))
            } else if (s.startsWith("'") && s.endsWith("'")) {
                result.add(s.substring(1, s.length - 1))
            } else {
                result.add(s)
            }
        }
        return result
    }

    override fun onDestroy() {
        disconnect()
        executor.shutdownNow()
        super.onDestroy()
    }

    // --- Foreground Notification Helpers ---
    private fun createNotificationChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val serviceChannel = NotificationChannel(
                CHANNEL_ID,
                "FFmpeg Encoding Service Channel",
                NotificationManager.IMPORTANCE_LOW
            )
            val manager = getSystemService(NotificationManager::class.java)
            manager?.createNotificationChannel(serviceChannel)
        }
    }

    private fun buildNotification(text: String): Notification {
        val notificationIntent = Intent(this, MainActivity::class.java)
        val pendingIntent = PendingIntent.getActivity(
            this, 0, notificationIntent,
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) PendingIntent.FLAG_IMMUTABLE else 0
        )

        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle("FFmpeg Distributed Worker")
            .setContentText(text)
            .setSmallIcon(android.R.drawable.ic_media_play)
            .setContentIntent(pendingIntent)
            .setOngoing(true)
            .build()
    }

    private fun updateNotification(text: String) {
        val notificationManager = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        notificationManager.notify(NOTIFICATION_ID, buildNotification(text))
    }
}
