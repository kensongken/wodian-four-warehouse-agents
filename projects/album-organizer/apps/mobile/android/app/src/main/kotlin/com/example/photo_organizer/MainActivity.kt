package com.example.photo_organizer

import com.example.photoorganizer.MediaBridge
import com.example.photoorganizer.BackgroundTransferBridge
import io.flutter.embedding.android.FlutterFragmentActivity
import io.flutter.embedding.engine.FlutterEngine

class MainActivity : FlutterFragmentActivity() {
    private var mediaBridge: MediaBridge? = null
    private var backgroundTransferBridge: BackgroundTransferBridge? = null

    override fun configureFlutterEngine(flutterEngine: FlutterEngine) {
        super.configureFlutterEngine(flutterEngine)
        mediaBridge = MediaBridge(this, flutterEngine.dartExecutor.binaryMessenger)
        backgroundTransferBridge = BackgroundTransferBridge(
            this,
            flutterEngine.dartExecutor.binaryMessenger,
        )
    }

    override fun cleanUpFlutterEngine(flutterEngine: FlutterEngine) {
        mediaBridge?.dispose()
        mediaBridge = null
        backgroundTransferBridge?.dispose()
        backgroundTransferBridge = null
        super.cleanUpFlutterEngine(flutterEngine)
    }
}
