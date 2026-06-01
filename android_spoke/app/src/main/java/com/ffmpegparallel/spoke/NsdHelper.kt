package com.ffmpegparallel.spoke

import android.content.Context
import android.net.nsd.NsdManager
import android.net.nsd.NsdServiceInfo
import android.util.Log

class NsdHelper(context: Context, private val onServiceResolved: (String, Int) -> Unit) {
    private val TAG = "NsdHelper"
    private val SERVICE_TYPE = "_ffmpeg-hub._tcp."
    private val nsdManager = context.getSystemService(Context.NSD_SERVICE) as NsdManager
    private var discoveryListener: NsdManager.DiscoveryListener? = null

    fun startDiscovery() {
        stopDiscovery()
        discoveryListener = object : NsdManager.DiscoveryListener {
            override fun onStartDiscoveryFailed(serviceType: String?, errorCode: Int) {
                Log.e(TAG, "Start discovery failed: $errorCode")
                try {
                    nsdManager.stopServiceDiscovery(this)
                } catch (e: Exception) { /* ignored */ }
            }

            override fun onStopDiscoveryFailed(serviceType: String?, errorCode: Int) {
                Log.e(TAG, "Stop discovery failed: $errorCode")
                try {
                    nsdManager.stopServiceDiscovery(this)
                } catch (e: Exception) { /* ignored */ }
            }

            override fun onDiscoveryStarted(serviceType: String?) {
                Log.d(TAG, "NSD Discovery started")
            }

            override fun onDiscoveryStopped(serviceType: String?) {
                Log.d(TAG, "NSD Discovery stopped")
            }

            override fun onServiceFound(serviceInfo: NsdServiceInfo?) {
                Log.d(TAG, "NSD Service found: ${serviceInfo?.serviceName}")
                val type = serviceInfo?.serviceType ?: ""
                
                // NsdManager serviceType sometimes has a dot at the end depending on Android version
                if (type.contains("ffmpeg-hub")) {
                    nsdManager.resolveService(serviceInfo, object : NsdManager.ResolveListener {
                        override fun onResolveFailed(serviceInfo: NsdServiceInfo?, errorCode: Int) {
                            Log.e(TAG, "NSD Resolve failed: $errorCode")
                        }

                        override fun onServiceResolved(resolvedServiceInfo: NsdServiceInfo?) {
                            Log.d(TAG, "NSD Resolve Succeeded: $resolvedServiceInfo")
                            val host = resolvedServiceInfo?.host?.hostAddress
                            val port = resolvedServiceInfo?.port
                            if (host != null && port != null) {
                                // Strip IPv6 zone index if present (e.g., %wlan0)
                                val cleanHost = if (host.contains("%")) host.substring(0, host.indexOf("%")) else host
                                onServiceResolved(cleanHost, port)
                            }
                        }
                    })
                }
            }

            override fun onServiceLost(serviceInfo: NsdServiceInfo?) {
                Log.d(TAG, "NSD Service lost: ${serviceInfo?.serviceName}")
            }
        }
        nsdManager.discoverServices(SERVICE_TYPE, NsdManager.PROTOCOL_DNS_SD, discoveryListener)
    }

    fun stopDiscovery() {
        if (discoveryListener != null) {
            try {
                nsdManager.stopServiceDiscovery(discoveryListener)
            } catch (e: Exception) {
                Log.e(TAG, "Failed to stop discovery: ${e.message}")
            }
            discoveryListener = null
        }
    }
}
