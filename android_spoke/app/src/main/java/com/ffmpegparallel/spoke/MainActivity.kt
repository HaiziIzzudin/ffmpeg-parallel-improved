package com.ffmpegparallel.spoke

import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.ServiceConnection
import android.content.res.ColorStateList
import android.graphics.Color
import android.graphics.Typeface
import android.os.Bundle
import android.os.IBinder
import android.text.InputType
import android.view.Gravity
import android.widget.*
import androidx.appcompat.app.AppCompatActivity

class MainActivity : AppCompatActivity(), EncodingService.StatusListener {
    private val TAG = "MainActivity"

    private var encodingService: EncodingService? = null
    private var isBound = false
    private lateinit var nsdHelper: NsdHelper
    private var isDiscoveryPaused = false

    // Programmatic UI Elements
    private lateinit var statusLabel: TextView
    private lateinit var discoveryLabel: TextView
    private lateinit var manualIpEdit: EditText
    private lateinit var connectBtn: Button
    private lateinit var disconnectBtn: Button
    private lateinit var logsContainer: TextView
    private lateinit var logsScroll: ScrollView

    private val serviceConnection = object : ServiceConnection {
        override fun onServiceConnected(name: ComponentName?, service: IBinder?) {
            val binder = service as EncodingService.LocalBinder
            encodingService = binder.getService()
            isBound = true
            encodingService?.setStatusListener(this@MainActivity)
            addLogToUi("Bound to encoding service.")
            
            // Start NSD to scan for PC Hub or keep it paused
            if (!isDiscoveryPaused) {
                updateDiscoveryState("Scanning local Wi-Fi for PC Hub...")
                nsdHelper.startDiscovery()
            } else {
                updateDiscoveryState("Auto-discovery paused.")
            }
            updateDisconnectButtonState()
        }

        override fun onServiceDisconnected(name: ComponentName?) {
            isBound = false
            encodingService = null
            addLogToUi("Service connection lost.")
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        
        // Build user interface programmatically (Dark Theme, rounded shapes)
        val mainLayout = createProgrammaticUi()
        setContentView(mainLayout)

        // Initialize NSD Helper
        nsdHelper = NsdHelper(this) { ip, port ->
            runOnUiThread {
                updateDiscoveryState("Discovered PC Hub at $ip:$port")
                addLogToUi("Auto-connecting to discovered PC Hub...")
                encodingService?.connectToHub(ip, port)
            }
        }

        // Start Foreground Service so it runs continuously
        val serviceIntent = Intent(this, EncodingService::class.java)
        if (android.os.Build.VERSION.SDK_INT >= android.os.Build.VERSION_CODES.O) {
            startForegroundService(serviceIntent)
        } else {
            startService(serviceIntent)
        }

        // Bind to Service
        bindService(serviceIntent, serviceConnection, Context.BIND_AUTO_CREATE)
    }

    override fun onStart() {
        super.onStart()
        if (isBound && !isDiscoveryPaused) {
            nsdHelper.startDiscovery()
        }
        updateDisconnectButtonState()
    }

    override fun onStop() {
        nsdHelper.stopDiscovery()
        super.onStop()
    }

    override fun onDestroy() {
        if (isBound) {
            encodingService?.setStatusListener(null)
            unbindService(serviceConnection)
            isBound = false
        }
        super.onDestroy()
    }

    // --- Programmatic Layout Construction ---
    private fun createProgrammaticUi(): LinearLayout {
        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setBackgroundColor(Color.parseColor("#11111b")) // Mocha Base dark background
            setPadding(dpToPx(16), dpToPx(16), dpToPx(16), dpToPx(16))
        }

        // App Title Banner
        val titleLabel = TextView(this).apply {
            text = "FFMPEG DISTRIBUTED SPOKE"
            textSize = 20f
            setTextColor(Color.parseColor("#cdd6f4")) // text color
            typeface = Typeface.create("sans-serif", Typeface.BOLD)
            gravity = Gravity.CENTER_HORIZONTAL
            layoutParams = LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT
            ).apply {
                setMargins(0, 0, 0, dpToPx(20))
            }
        }
        root.addWidget(titleLabel)

        // Section 1: Connection Panel
        val connGroup = createCardLayout().apply {
            addWidget(createSectionTitle("PC HUB CONNECTION"))
            
            discoveryLabel = TextView(this@MainActivity).apply {
                text = "Initializing discovery..."
                setTextColor(Color.parseColor("#f9e2af")) // yellow status
                textSize = 14f
                setPadding(0, 0, 0, dpToPx(8))
            }
            addWidget(discoveryLabel)
            
            // Manual IP input
            val manualLayout = LinearLayout(this@MainActivity).apply {
                orientation = LinearLayout.HORIZONTAL
                gravity = Gravity.CENTER_VERTICAL
            }
            
            manualIpEdit = EditText(this@MainActivity).apply {
                hint = "Enter IP address manually"
                setHintTextColor(Color.parseColor("#7f849c"))
                setTextColor(Color.WHITE)
                inputType = InputType.TYPE_CLASS_TEXT
                textSize = 14f
                layoutParams = LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1.0f)
            }
            manualLayout.addWidget(manualIpEdit)
            
            connectBtn = Button(this@MainActivity).apply {
                text = "Connect"
                setTextColor(Color.parseColor("#11111b"))
                backgroundTintList = ColorStateList.valueOf(Color.parseColor("#89b4fa")) // Blue
                textSize = 12f
                setOnClickListener {
                    val ip = manualIpEdit.text.toString().trim()
                    if (ip.isNotEmpty()) {
                        addLogToUi("Manually connecting to $ip:8000...")
                        encodingService?.connectToHub(ip, 8000)
                    } else {
                        Toast.makeText(this@MainActivity, "Please enter an IP", Toast.LENGTH_SHORT).show()
                    }
                }
            }
            manualLayout.addWidget(connectBtn)
            addWidget(manualLayout)
        }
        root.addWidget(connGroup)

        // Section 2: Worker Status Panel
        val statusGroup = createCardLayout().apply {
            addWidget(createSectionTitle("WORKER STATE"))
            
            statusLabel = TextView(this@MainActivity).apply {
                text = "Disconnected"
                setTextColor(Color.parseColor("#f38ba8")) // red status
                textSize = 16f
                typeface = Typeface.create("sans-serif", Typeface.BOLD)
                setPadding(0, 0, 0, dpToPx(8))
            }
            addWidget(statusLabel)
            
            disconnectBtn = Button(this@MainActivity).apply {
                setTextColor(Color.parseColor("#11111b"))
                textSize = 12f
            }
            updateDisconnectButtonState()
            addWidget(disconnectBtn)
        }
        root.addWidget(statusGroup)

        // Section 3: Logs View Panel
        val logGroup = createCardLayout().apply {
            layoutParams = LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                0,
                1.0f
            ).apply {
                setMargins(0, dpToPx(10), 0, 0)
            }
            addWidget(createSectionTitle("CONSOLE LOGS"))
            
            logsScroll = ScrollView(this@MainActivity).apply {
                layoutParams = LinearLayout.LayoutParams(
                    LinearLayout.LayoutParams.MATCH_PARENT,
                    LinearLayout.LayoutParams.MATCH_PARENT
                )
                setBackgroundColor(Color.parseColor("#181825")) // darker box
                setPadding(dpToPx(8), dpToPx(8), dpToPx(8), dpToPx(8))
            }
            
            logsContainer = TextView(this@MainActivity).apply {
                text = ""
                setTextColor(Color.parseColor("#a6e3a1")) // green logs
                textSize = 11f
                typeface = Typeface.MONOSPACE
            }
            logsScroll.addView(logsContainer)
            addWidget(logsScroll)
        }
        root.addWidget(logGroup)

        return root
    }

    private fun createCardLayout(): LinearLayout {
        return LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setBackgroundColor(Color.parseColor("#1e1e2e")) // card bg
            setPadding(dpToPx(14), dpToPx(14), dpToPx(14), dpToPx(14))
            
            val params = LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT
            )
            params.setMargins(0, 0, 0, dpToPx(12))
            layoutParams = params
            
            // Minimal programmatic border drawable via standard colors (simulated by setting background)
            // To be robust across older android SDKs, we just rely on padding and solid dark background card colors
        }
    }

    private fun createSectionTitle(titleText: String): TextView {
        return TextView(this).apply {
            text = titleText
            setTextColor(Color.parseColor("#a6adc8")) // lavender subtext
            textSize = 11f
            typeface = Typeface.create("sans-serif", Typeface.BOLD)
            setPadding(0, 0, 0, dpToPx(6))
        }
    }

    private fun dpToPx(dp: Int): Int {
        val density = resources.displayMetrics.density
        return (dp * density).toInt()
    }

    private fun updateDiscoveryState(state: String) {
        discoveryLabel.text = state
    }

    // Helper extension to add Views to layouts cleanly
    private fun LinearLayout.addWidget(view: android.view.View) {
        this.addView(view)
    }

    private fun addLogToUi(log: String) {
        runOnUiThread {
            logsContainer.append("${time_now()} - $log\n")
            logsScroll.post {
                logsScroll.fullScroll(ScrollView.FOCUS_DOWN)
            }
        }
    }

    private fun time_now(): String {
        val formatter = java.text.SimpleDateFormat("HH:mm:ss", java.util.Locale.getDefault())
        return formatter.format(java.util.Date())
    }

    // --- EncodingService.StatusListener Callbacks ---
    override fun onStatusChanged(status: String) {
        runOnUiThread {
            statusLabel.text = status
            if (status.contains("Idle") || status.contains("Ready")) {
                statusLabel.setTextColor(Color.parseColor("#a6e3a1")) // Green
            } else if (status.contains("Connecting") || status.contains("Downloading") || status.contains("Encoding")) {
                statusLabel.setTextColor(Color.parseColor("#f9e2af")) // Yellow
            } else if (status.contains("Disconnected")) {
                statusLabel.setTextColor(Color.parseColor("#f38ba8")) // Red
            } else {
                statusLabel.setTextColor(Color.WHITE)
            }
            updateDisconnectButtonState()
        }
    }

    private fun updateDisconnectButtonState() {
        val service = encodingService
        val connected = service?.isConnected == true
        if (connected || !isDiscoveryPaused) {
            disconnectBtn.text = "Disconnect Client"
            disconnectBtn.backgroundTintList = ColorStateList.valueOf(Color.parseColor("#f38ba8")) // Red
            disconnectBtn.setOnClickListener {
                encodingService?.disconnect()
                nsdHelper.stopDiscovery()
                isDiscoveryPaused = true
                updateDiscoveryState("Auto-discovery paused.")
                updateDisconnectButtonState()
            }
        } else {
            disconnectBtn.text = "Start Auto-Discovery"
            disconnectBtn.backgroundTintList = ColorStateList.valueOf(Color.parseColor("#a6e3a1")) // Green
            disconnectBtn.setOnClickListener {
                isDiscoveryPaused = false
                updateDiscoveryState("Scanning local Wi-Fi for PC Hub...")
                nsdHelper.startDiscovery()
                updateDisconnectButtonState()
            }
        }
    }

    override fun onLogAdded(log: String) {
        addLogToUi(log)
    }
}
