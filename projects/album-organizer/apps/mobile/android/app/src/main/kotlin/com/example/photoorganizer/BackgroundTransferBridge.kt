package com.example.photoorganizer

import android.content.Context
import android.net.NetworkCapabilities
import android.net.NetworkRequest
import android.os.Handler
import android.os.Looper
import androidx.lifecycle.LiveData
import androidx.lifecycle.Observer
import androidx.work.Constraints
import androidx.work.Data
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.WorkInfo
import androidx.work.WorkManager
import io.flutter.plugin.common.BinaryMessenger
import io.flutter.plugin.common.EventChannel
import io.flutter.plugin.common.MethodCall
import io.flutter.plugin.common.MethodChannel
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicBoolean

class BackgroundTransferBridge(
    context: Context,
    messenger: BinaryMessenger,
) : MethodChannel.MethodCallHandler, EventChannel.StreamHandler {
    private val appContext = context.applicationContext
    private val workManager = WorkManager.getInstance(appContext)
    private val methods = MethodChannel(messenger, METHOD_CHANNEL)
    private val events = EventChannel(messenger, EVENT_CHANNEL)
    private val executor = Executors.newSingleThreadExecutor()
    private val main = Handler(Looper.getMainLooper())
    private val disposed = AtomicBoolean(false)
    private val observers = mutableMapOf<String, WorkObserver>()
    private var eventSink: EventChannel.EventSink? = null

    init {
        methods.setMethodCallHandler(this)
        events.setStreamHandler(this)
    }

    override fun onMethodCall(call: MethodCall, result: MethodChannel.Result) {
        if (disposed.get()) {
            result.error("bridge_disposed", "后台上传暂时不可用", null)
            return
        }
        when (call.method) {
            "enqueuePart" -> enqueue(call.arguments, result)
            "queryJobs" -> query(call.arguments, result)
            "acknowledge" -> acknowledge(call.arguments, result)
            "cancel" -> cancel(call.arguments, result)
            else -> result.notImplemented()
        }
    }

    override fun onListen(arguments: Any?, sink: EventChannel.EventSink) {
        eventSink = sink
    }

    override fun onCancel(arguments: Any?) {
        eventSink = null
    }

    fun dispose() {
        if (!disposed.compareAndSet(false, true)) return
        methods.setMethodCallHandler(null)
        events.setStreamHandler(null)
        synchronized(observers) {
            observers.values.forEach { registration ->
                registration.liveData.removeObserver(registration.observer)
            }
            observers.clear()
        }
        executor.shutdown()
    }

    private fun enqueue(arguments: Any?, result: MethodChannel.Result) {
        val request = runCatching { EnqueueRequest(arguments) }.getOrElse {
            result.error("invalid_arguments", "后台上传参数无效", null)
            return
        }
        executor.execute {
            try {
                val existing = workManager.getWorkInfosForUniqueWork(uniqueName(request.jobId))
                    .get()
                    .firstOrNull { it.state != WorkInfo.State.CANCELLED }
                if (existing == null) {
                    SignedUrlVault(appContext).put(request.jobId, request.signedUrl)
                    val constraints = transferConstraints(request.allowMobileData)
                    val work = OneTimeWorkRequestBuilder<UploadPartWorker>()
                        .setConstraints(constraints)
                        .setInputData(request.workerData)
                        .addTag(uniqueName(request.jobId))
                        .build()
                    try {
                        workManager.enqueueUniqueWork(
                            uniqueName(request.jobId),
                            ExistingWorkPolicy.KEEP,
                            work,
                        ).result.get()
                    } catch (error: Exception) {
                        SignedUrlVault(appContext).remove(request.jobId)
                        throw error
                    }
                }
                observe(request.jobId)
                success(result, request.jobId)
            } catch (_: Exception) {
                failure(result, "enqueue_failed", "无法安排后台上传")
            }
        }
    }

    private fun query(arguments: Any?, result: MethodChannel.Result) {
        val ids = validatedQueryJobIds(arguments)
        if (ids == null) {
            result.error("invalid_arguments", "后台上传参数无效", null)
            return
        }
        executor.execute {
            try {
                val payload = ids.mapNotNull { raw ->
                    val jobId = raw
                    val info = workManager.getWorkInfosForUniqueWork(uniqueName(jobId))
                        .get()
                        .firstOrNull { it.state != WorkInfo.State.CANCELLED }
                    if (info != null && shouldObserve(info.state)) observe(jobId)
                    info?.let { event(jobId, it) }
                }
                success(result, payload)
            } catch (_: Exception) {
                failure(result, "query_failed", "无法读取后台上传状态")
            }
        }
    }

    private fun acknowledge(arguments: Any?, result: MethodChannel.Result) {
        val jobId = jobId(arguments) ?: run {
            result.error("invalid_arguments", "后台上传参数无效", null)
            return
        }
        if (!SignedUrlVault(appContext).remove(jobId)) {
            result.error("persistence_failure", "无法清理后台上传凭证", null)
            return
        }
        removeObserver(jobId)
        result.success(null)
    }

    private fun cancel(arguments: Any?, result: MethodChannel.Result) {
        val jobId = jobId(arguments) ?: run {
            result.error("invalid_arguments", "后台上传参数无效", null)
            return
        }
        executor.execute {
            try {
                workManager.cancelUniqueWork(uniqueName(jobId)).result.get()
                if (!SignedUrlVault(appContext).remove(jobId)) {
                    failure(result, "persistence_failure", "无法清理后台上传凭证")
                    return@execute
                }
                removeObserver(jobId)
                success(result, null)
            } catch (_: Exception) {
                failure(result, "cancel_failed", "无法取消后台上传")
            }
        }
    }

    private fun observe(jobId: String) {
        synchronized(observers) {
            if (observers.containsKey(jobId)) return
            val observer = Observer<List<WorkInfo>> { values ->
                val info = values.firstOrNull { it.state != WorkInfo.State.CANCELLED }
                    ?: return@Observer
                val payload = event(jobId, info)
                main.post { eventSink?.success(payload) }
            }
            val liveData = workManager.getWorkInfosForUniqueWorkLiveData(uniqueName(jobId))
            val registration = WorkObserver(liveData, observer)
            observers[jobId] = registration
            main.post {
                if (!disposed.get()) {
                    registration.liveData.observeForever(registration.observer)
                } else {
                    synchronized(observers) { observers.remove(jobId) }
                }
            }
        }
    }

    private fun removeObserver(jobId: String) {
        val registration = synchronized(observers) { observers.remove(jobId) } ?: return
        main.post { registration.liveData.removeObserver(registration.observer) }
    }

    private fun event(jobId: String, info: WorkInfo): Map<String, Any> {
        val output = info.outputData
        return when (info.state) {
            WorkInfo.State.ENQUEUED, WorkInfo.State.BLOCKED ->
                mapOf("jobId" to jobId, "state" to "queued")
            WorkInfo.State.RUNNING -> {
                val progress = info.progress.getInt(UploadPartWorker.KEY_PROGRESS, -1)
                if (progress in 0..100) {
                    mapOf("jobId" to jobId, "state" to "progress", "progress" to progress)
                } else {
                    mapOf("jobId" to jobId, "state" to "running")
                }
            }
            WorkInfo.State.SUCCEEDED ->
                mapOf(
                    "jobId" to jobId,
                    "state" to "succeeded",
                    "etag" to output.getString(UploadPartWorker.KEY_ETAG).orEmpty(),
                )
            WorkInfo.State.FAILED ->
                mapOf(
                    "jobId" to jobId,
                    "state" to "failed",
                    "code" to
                        (output.getString(UploadPartWorker.KEY_CODE) ?: "service_unavailable"),
                    "retryable" to output.getBoolean(UploadPartWorker.KEY_RETRYABLE, false),
                )
            WorkInfo.State.CANCELLED ->
                mapOf(
                    "jobId" to jobId,
                    "state" to "failed",
                    "code" to "background_expired",
                    "retryable" to true,
                )
        }
    }

    private fun success(result: MethodChannel.Result, value: Any?) {
        main.post { if (!disposed.get()) result.success(value) }
    }

    private fun failure(result: MethodChannel.Result, code: String, message: String) {
        main.post { if (!disposed.get()) result.error(code, message, null) }
    }

    private fun jobId(arguments: Any?): String? {
        val value = (arguments as? Map<*, *>)?.get("jobId") as? String
        return value?.takeIf(::safeJobId)
    }

    private data class EnqueueRequest(
        val jobId: String,
        val assetId: String,
        val accessKind: String,
        val locator: String,
        val byteOffset: Long,
        val byteLength: Long,
        val signedUrl: String,
        val allowMobileData: Boolean,
    ) {
        constructor(arguments: Any?) : this(
            jobId = requiredString(arguments, "jobId"),
            assetId = requiredAssetString(arguments, "id"),
            accessKind = requiredAssetString(arguments, "accessKind"),
            locator = requiredAssetString(arguments, "locator"),
            byteOffset = requiredNumber(arguments, "byteOffset"),
            byteLength = requiredNumber(arguments, "byteLength"),
            signedUrl = requiredString(arguments, "signedUrl"),
            allowMobileData = requiredBoolean(arguments, "allowMobileData"),
        ) {
            require(safeJobId(jobId))
            require(accessKind == "stagedFile" || accessKind == "contentUri")
            require(byteOffset >= 0 && byteLength > 0)
            val uri = android.net.Uri.parse(signedUrl)
            require(uri.scheme == "https" && !uri.host.isNullOrEmpty())
        }

        val workerData: Data
            get() = Data.Builder()
                .putString(UploadPartWorker.KEY_JOB_ID, jobId)
                .putString(UploadPartWorker.KEY_ASSET_ID, assetId)
                .putString(UploadPartWorker.KEY_ACCESS_KIND, accessKind)
                .putString(UploadPartWorker.KEY_LOCATOR, locator)
                .putLong(UploadPartWorker.KEY_OFFSET, byteOffset)
                .putLong(UploadPartWorker.KEY_LENGTH, byteLength)
                .putBoolean(UploadPartWorker.KEY_ALLOW_MOBILE_DATA, allowMobileData)
                .build()
    }

    private data class WorkObserver(
        val liveData: LiveData<List<WorkInfo>>,
        val observer: Observer<List<WorkInfo>>,
    )

    companion object {
        private const val METHOD_CHANNEL = "photo_organizer/background_transfer"
        private const val EVENT_CHANNEL = "photo_organizer/background_transfer_events"
        internal const val MAXIMUM_QUERY_JOB_IDS = 10000

        @JvmStatic
        internal fun safeJobId(value: String): Boolean =
            value.isNotEmpty() && value.length <= 512 && value.none(Char::isISOControl)

        @JvmStatic
        @Suppress("UNCHECKED_CAST")
        internal fun validatedQueryJobIds(arguments: Any?): List<String>? {
            val ids = (arguments as? Map<*, *>)?.get("jobIds") as? List<*> ?: return null
            if (
                ids.size > MAXIMUM_QUERY_JOB_IDS ||
                ids.any { it !is String || !safeJobId(it) }
            ) {
                return null
            }
            return ids as List<String>
        }

        @JvmStatic
        internal fun uniqueName(jobId: String): String = UploadPartWorker.uniqueWorkName(jobId)

        @JvmStatic
        internal fun transferConstraints(allowMobileData: Boolean): Constraints {
            val builder = Constraints.Builder()
            val transports = requiredTransports(allowMobileData)
            if (transports.isEmpty()) {
                return builder.setRequiredNetworkType(NetworkType.CONNECTED).build()
            }
            val requestBuilder = NetworkRequest.Builder()
                .addCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET)
            transports.forEach(requestBuilder::addTransportType)
            return builder.setRequiredNetworkRequest(
                requestBuilder.build(),
                NetworkType.CONNECTED,
            ).build()
        }

        @JvmStatic
        internal fun requiredTransports(allowMobileData: Boolean): Set<Int> =
            if (allowMobileData) {
                emptySet()
            } else {
                setOf(
                    NetworkCapabilities.TRANSPORT_WIFI,
                    NetworkCapabilities.TRANSPORT_ETHERNET,
                )
            }

        @JvmStatic
        internal fun shouldObserve(state: WorkInfo.State): Boolean =
            state == WorkInfo.State.ENQUEUED ||
                state == WorkInfo.State.BLOCKED ||
                state == WorkInfo.State.RUNNING

        private fun requiredString(arguments: Any?, key: String): String =
            ((arguments as? Map<*, *>)?.get(key) as? String)?.takeIf(String::isNotEmpty)
                ?: throw IllegalArgumentException(key)

        private fun requiredAssetString(arguments: Any?, key: String): String =
            ((((arguments as? Map<*, *>)?.get("asset") as? Map<*, *>)?.get(key)) as? String)
                ?.takeIf(String::isNotEmpty)
                ?: throw IllegalArgumentException(key)

        private fun requiredNumber(arguments: Any?, key: String): Long =
            ((arguments as? Map<*, *>)?.get(key) as? Number)?.toLong()
                ?: throw IllegalArgumentException(key)

        private fun requiredBoolean(arguments: Any?, key: String): Boolean =
            (arguments as? Map<*, *>)?.get(key) as? Boolean
                ?: throw IllegalArgumentException(key)
    }
}
