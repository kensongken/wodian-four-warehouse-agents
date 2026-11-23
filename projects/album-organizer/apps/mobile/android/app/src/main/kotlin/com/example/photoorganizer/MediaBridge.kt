package com.example.photoorganizer

import android.annotation.SuppressLint
import android.content.Intent
import android.content.Context
import android.net.Uri
import android.os.Build
import android.os.ext.SdkExtensions
import android.provider.MediaStore
import android.provider.OpenableColumns
import android.util.AtomicFile
import androidx.activity.result.ActivityResultLauncher
import androidx.activity.result.PickVisualMediaRequest
import androidx.activity.result.contract.ActivityResultContracts
import com.example.photo_organizer.MainActivity
import io.flutter.plugin.common.BinaryMessenger
import io.flutter.plugin.common.MethodCall
import io.flutter.plugin.common.MethodChannel
import java.io.File
import java.io.FileNotFoundException
import java.io.FileOutputStream
import java.io.InputStream
import java.security.MessageDigest
import java.util.Properties
import java.util.UUID
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicBoolean
import kotlin.math.min

class MediaBridge(
    private val activity: MainActivity,
    messenger: BinaryMessenger,
) : MethodChannel.MethodCallHandler {
    private val channel = MethodChannel(messenger, CHANNEL_NAME)
    private val executor = Executors.newSingleThreadExecutor()
    private val leaseRegistry = PersistedLeaseRegistry(
        AtomicFileLeaseStore(File(activity.filesDir, "owned_media_refs.properties")),
    )
    private val disposed = AtomicBoolean(false)
    private var pendingPick: PendingPick? = null
    private var activeLauncher: ActivityResultLauncher<PickVisualMediaRequest>? = null

    init {
        runCatching { leaseRegistry.reconcilePending(::dropPersistedGrant) }
        channel.setMethodCallHandler(this)
    }

    override fun onMethodCall(call: MethodCall, result: MethodChannel.Result) {
        val reply = ReplyOnce(result)
        if (disposed.get()) {
            reply.error("activity_recreated", "照片选择已中断，请重新选择", null)
            return
        }
        when (call.method) {
            "pickImages" -> pickImages(call, reply)
            "computeSha256" -> runFileOperation(reply) { computeSha256(call.arguments) }
            "release" -> runFileOperation(reply) {
                release(call.arguments)
                null
            }
            else -> reply.notImplemented()
        }
    }

    fun dispose() {
        if (!disposed.compareAndSet(false, true)) return
        channel.setMethodCallHandler(null)
        activeLauncher?.unregister()
        activeLauncher = null
        val pending = pendingPick
        pendingPick = null
        pending?.reply?.error("activity_recreated", "照片选择已中断，请重新选择", null)
        pending?.coordinator?.dispose { abandoned ->
            executor.execute { abandoned.forEach(::releaseOwnedRef) }
        }
        executor.shutdown()
    }

    private fun pickImages(call: MethodCall, reply: ReplyOnce) {
        val remainingSlots = call.argument<Int>("remainingSlots")
        if (remainingSlots == null || !isValidRemainingSlots(remainingSlots)) {
            reply.error("invalid_arguments", "选择数量无效", null)
            return
        }
        if (disposed.get()) {
            reply.error("activity_recreated", "照片选择已中断，请重新选择", null)
            return
        }
        if (pendingPick != null) {
            reply.error("picker_busy", "照片选择器正在使用中", null)
            return
        }

        val limit = currentPickerLimit(remainingSlots)
        val pending = PendingPick(reply, limit, PendingSelectionCoordinator(::releaseOwnedRef))
        pendingPick = pending
        try {
            activeLauncher =
                if (limit == 1) {
                    activity.activityResultRegistry.register(
                        PICKER_KEY,
                        ActivityResultContracts.PickVisualMedia(),
                    ) { uri -> completePicker(pending, uri?.let(::listOf) ?: emptyList()) }
                } else {
                    activity.activityResultRegistry.register(
                        PICKER_KEY,
                        ActivityResultContracts.PickMultipleVisualMedia(limit),
                    ) { uris -> completePicker(pending, uris) }
                }
            activeLauncher?.launch(
                PickVisualMediaRequest(ActivityResultContracts.PickVisualMedia.ImageOnly),
            )
        } catch (_: RuntimeException) {
            activeLauncher?.unregister()
            activeLauncher = null
            if (pendingPick === pending) pendingPick = null
            reply.error("picker_unavailable", "暂时无法打开照片选择器", null)
        }
    }

    private fun completePicker(pending: PendingPick, uris: List<Uri>) {
        if (pendingPick !== pending || !pending.processing.compareAndSet(false, true)) return
        activeLauncher?.unregister()
        activeLauncher = null
        if (uris.size > pending.limit) {
            pendingPick = null
            pending.reply.error("invalid_picker_result", "照片选择结果超出任务上限", null)
            return
        }
        executor.execute {
            val payload = processUris(pending, uris)
            activity.runOnUiThread {
                if (
                    pendingPick === pending &&
                    !disposed.get()
                ) {
                    pending.coordinator.transferToFlutter {
                        pendingPick = null
                        pending.reply.success(payload)
                    }
                }
            }
        }
    }

    private fun processUris(pending: PendingPick, uris: List<Uri>): Map<String, Any> {
        val selected = mutableListOf<Map<String, Any>>()
        val unsupported = mutableListOf<Map<String, Any?>>()
        for (uri in uris) {
            when (val outcome = processUri(uri)) {
                is ItemOutcome.Selected -> {
                    if (pending.coordinator.acquire(outcome.ref)) {
                        selected += outcome.value
                    }
                }
                is ItemOutcome.Unsupported -> unsupported += outcome.value
            }
        }
        return mapOf("selected" to selected, "unsupported" to unsupported)
    }

    private fun processUri(uri: Uri): ItemOutcome {
        val id = "android:${digestText(uri.toString())}"
        val metadata = readMetadata(uri)
        val name = safeDisplayName(metadata?.displayName)
        val rawMediaType = runCatching { activity.contentResolver.getType(uri) }.getOrNull()
        val mediaType = normalizedMediaType(rawMediaType)
            ?: return unsupported(id, name, "unsupported_media_type", rawMediaType, metadata?.size)
        val knownSize = metadata?.size
        if (knownSize != null && knownSize > MAXIMUM_ASSET_BYTES) {
            return unsupported(id, name, "image_too_large", mediaType, knownSize)
        }

        return try {
            // Provider metadata is only an optimization; stream-count before
            // acceptance so an incorrect OpenableColumns.SIZE cannot bypass the cap.
            val byteSize = countBytes(uri)
            if (byteSize > MAXIMUM_ASSET_BYTES) {
                return unsupported(id, name, "image_too_large", mediaType, byteSize)
            }
            if (byteSize <= 0) {
                return unsupported(id, name, "read_failure", mediaType, byteSize)
            }
            var stagedSize: Long? = null
            val leaseToken = UUID.randomUUID().toString().lowercase()
            val access = chooseDurableAccess(
                persist = { persist(uri, id, leaseToken) },
                stage = {
                    val staged = stage(uri, id.removePrefix("android:"), leaseToken)
                    stagedSize = staged.byteSize
                    staged.locator
                },
            )
            val durableSize = stagedSize ?: byteSize
            if (durableSize > MAXIMUM_ASSET_BYTES) {
                if (access.kind == "stagedFile") {
                    runCatching { releaseStagedFile(id, access.locator, stagingRoot()) }
                } else {
                    releasePersisted(id, access.locator)
                }
                unsupported(id, name, "image_too_large", mediaType, durableSize)
            } else {
                val ref = AssetRef(id, access.kind, access.locator)
                ItemOutcome.Selected(
                    value = mapOf(
                        "id" to id,
                        "displayName" to name,
                        "mediaType" to mediaType,
                        "byteSize" to durableSize,
                        "accessKind" to access.kind,
                        "locator" to access.locator,
                    ),
                    ref = ref,
                )
            }
        } catch (tooLarge: AssetTooLarge) {
            unsupported(id, name, "image_too_large", mediaType, tooLarge.byteSize)
        } catch (_: Exception) {
            unsupported(id, name, "read_failure", mediaType, knownSize)
        }
    }

    private fun readMetadata(uri: Uri): AssetMetadata? =
        try {
            activity.contentResolver.query(
                uri,
                arrayOf(OpenableColumns.DISPLAY_NAME, OpenableColumns.SIZE),
                null,
                null,
                null,
            )?.use { cursor ->
                if (!cursor.moveToFirst()) return@use null
                val nameColumn = cursor.getColumnIndex(OpenableColumns.DISPLAY_NAME)
                val sizeColumn = cursor.getColumnIndex(OpenableColumns.SIZE)
                AssetMetadata(
                    displayName =
                        if (nameColumn >= 0 && !cursor.isNull(nameColumn)) cursor.getString(nameColumn)
                        else null,
                    size =
                        if (sizeColumn >= 0 && !cursor.isNull(sizeColumn)) {
                            nonNegativeSize(cursor.getLong(sizeColumn))
                        }
                        else null,
                )
            }
        } catch (_: RuntimeException) {
            null
        }

    private fun persist(uri: Uri, id: String, leaseToken: String): String? =
        leaseRegistry.acquire(
            id = id,
            rawUri = uri.toString(),
            leaseToken = leaseToken,
            acquireGrant = {
                try {
                    activity.contentResolver.takePersistableUriPermission(
                        uri,
                        Intent.FLAG_GRANT_READ_URI_PERMISSION,
                    )
                    true
                } catch (_: SecurityException) {
                    false
                } catch (_: UnsupportedOperationException) {
                    false
                }
            },
            rollbackGrant = {
                dropPersistedGrant(uri.toString())
            },
        )

    private fun stage(uri: Uri, token: String, leaseToken: String): StagedAsset {
        val input = activity.contentResolver.openInputStream(uri)
            ?: throw MediaBridgeException("read_failure")
        return input.use { source ->
            stageOwnedStream(source, token, leaseToken, stagingRoot())
        }
    }

    private fun countBytes(uri: Uri): Long {
        val input = activity.contentResolver.openInputStream(uri)
            ?: throw MediaBridgeException("read_failure")
        input.use { source ->
            val buffer = ByteArray(HASH_BUFFER_BYTES)
            var total = 0L
            while (true) {
                val read = source.read(buffer)
                if (read < 0) return total
                total += read
                if (total > MAXIMUM_ASSET_BYTES) return total
            }
        }
    }

    private fun computeSha256(arguments: Any?): String {
        val ref = assetRef(arguments)
        val input =
            when (ref.accessKind) {
                "contentUri" -> {
                    val rawUri = leaseRegistry.resolve(ref.id, ref.locator)
                        ?: throw SecurityException("invalid asset reference")
                    activity.contentResolver.openInputStream(Uri.parse(rawUri))
                }
                "stagedFile" -> validatedStagedFile(ref.id, ref.locator, stagingRoot()).inputStream()
                else -> null
            } ?: throw SecurityException("invalid asset reference")
        input.use { source ->
            val digest = MessageDigest.getInstance("SHA-256")
            val buffer = ByteArray(HASH_BUFFER_BYTES)
            while (true) {
                val read = source.read(buffer)
                if (read < 0) break
                digest.update(buffer, 0, read)
            }
            return digest.digest().joinToString("") { "%02x".format(it) }
        }
    }

    private fun release(arguments: Any?) {
        val ref = assetRef(arguments)
        when (ref.accessKind) {
            "contentUri" -> releasePersisted(ref.id, ref.locator)
            "stagedFile" -> releaseStagedFile(ref.id, ref.locator, stagingRoot())
            else -> throw SecurityException("invalid asset reference")
        }
    }

    private fun releasePersisted(id: String, locator: String) {
        if (!SAFE_ID.matches(id)) {
            throw SecurityException("invalid asset reference")
        }
        leaseRegistry.release(id, locator) { rawUri ->
            dropPersistedGrant(rawUri)
        }
    }

    private fun dropPersistedGrant(rawUri: String) {
        try {
            activity.contentResolver.releasePersistableUriPermission(
                Uri.parse(rawUri),
                Intent.FLAG_GRANT_READ_URI_PERMISSION,
            )
        } catch (_: SecurityException) {
            // The provider already revoked this verified, app-owned grant.
        }
    }

    private fun releaseOwnedRef(ref: AssetRef) {
        runCatching {
            when (ref.accessKind) {
                "contentUri" -> releasePersisted(ref.id, ref.locator)
                "stagedFile" -> releaseStagedFile(ref.id, ref.locator, stagingRoot())
                else -> throw SecurityException("invalid asset reference")
            }
        }
    }

    private fun stagingRoot(): File = File(activity.filesDir, "upload_staging")

    private fun runFileOperation(reply: ReplyOnce, operation: () -> Any?) {
        executor.execute {
            if (disposed.get()) {
                activity.runOnUiThread {
                    reply.error("activity_recreated", "照片选择已中断，请重新选择", null)
                }
                return@execute
            }
            try {
                val value = operation()
                activity.runOnUiThread {
                    if (disposed.get()) {
                        reply.error("activity_recreated", "照片选择已中断，请重新选择", null)
                    } else {
                        reply.success(value)
                    }
                }
            } catch (_: SecurityException) {
                activity.runOnUiThread { reply.error("invalid_asset_ref", "照片引用无效", null) }
            } catch (_: Exception) {
                activity.runOnUiThread { reply.error("read_failure", "无法读取照片", null) }
            }
        }
    }

    private fun assetRef(arguments: Any?): AssetRef {
        val map = arguments as? Map<*, *> ?: throw SecurityException("invalid asset reference")
        val id = map["id"] as? String ?: throw SecurityException("invalid asset reference")
        val accessKind = map["accessKind"] as? String
            ?: throw SecurityException("invalid asset reference")
        val locator = map["locator"] as? String ?: throw SecurityException("invalid asset reference")
        return AssetRef(id, accessKind, locator)
    }

    private fun unsupported(
        id: String,
        name: String,
        reason: String,
        mediaType: String?,
        byteSize: Long?,
    ): ItemOutcome.Unsupported =
        ItemOutcome.Unsupported(
            buildMap {
                put("id", id)
                put("name", name)
                put("reason", reason)
                if (mediaType != null) put("mediaType", mediaType)
                if (byteSize != null) put("byteSize", byteSize)
            },
        )

    companion object {
        private const val CHANNEL_NAME = "photo_organizer/media_library"
        private const val PICKER_KEY = "photo_organizer.media_library.picker"
        internal const val MAXIMUM_ACCEPTED_ASSETS = 10000
        private const val HASH_BUFFER_BYTES = 1024 * 1024
        private const val MAXIMUM_ASSET_BYTES = 100L * 1024L * 1024L
        private val SAFE_TOKEN = Regex("^[0-9a-f]{64}$")
        private val SAFE_LEASE_TOKEN =
            Regex("^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
        private val SAFE_ID = Regex("^android:[0-9a-f]{64}$")

        @JvmStatic
        internal fun normalizedMediaType(value: String?): String? =
            when (value?.lowercase()) {
                "image/heic", "image/heif" -> "image/heic"
                "image/jpeg", "image/jpg" -> "image/jpeg"
                "image/png" -> "image/png"
                else -> null
            }

        @JvmStatic
        internal fun nonNegativeSize(value: Long?): Long? = value?.takeIf { it >= 0 }

        @JvmStatic
        internal fun isValidRemainingSlots(value: Int): Boolean =
            value in 1..MAXIMUM_ACCEPTED_ASSETS

        @JvmStatic
        internal fun pickerLimit(remainingSlots: Int, deviceMaximum: Int): Int {
            require(remainingSlots > 0 && deviceMaximum > 0)
            return min(remainingSlots, deviceMaximum)
        }

        @JvmStatic
        internal fun publicPickerLimit(
            remainingSlots: Int,
            sdkInt: Int,
            rExtensionVersion: Int,
            platformMaximum: () -> Int,
        ): Int {
            val frameworkPickerAvailable =
                sdkInt >= Build.VERSION_CODES.TIRAMISU ||
                    (sdkInt >= Build.VERSION_CODES.R && rExtensionVersion >= 2)
            return if (frameworkPickerAvailable) {
                pickerLimit(remainingSlots, platformMaximum())
            } else {
                remainingSlots
            }
        }

        @SuppressLint("NewApi")
        @JvmStatic
        internal fun currentPickerLimit(
            remainingSlots: Int,
            sdkInt: Int = Build.VERSION.SDK_INT,
            rExtensionVersion: Int =
                if (sdkInt >= Build.VERSION_CODES.R) {
                    SdkExtensions.getExtensionVersion(Build.VERSION_CODES.R)
                } else {
                    0
                },
            platformMaximum: () -> Int = { MediaStore.getPickImagesMaxLimit() },
        ): Int =
            publicPickerLimit(
                remainingSlots = remainingSlots,
                sdkInt = sdkInt,
                rExtensionVersion = rExtensionVersion,
                platformMaximum = platformMaximum,
            )

        @JvmStatic
        internal fun chooseDurableAccess(
            persist: () -> String?,
            stage: () -> String,
        ): DurableAccess {
            val persistedLocator = persist()
            return if (persistedLocator != null) {
                DurableAccess("contentUri", persistedLocator)
            } else {
                DurableAccess("stagedFile", stage())
            }
        }

        @JvmStatic
        internal fun stageOwnedStream(
            source: InputStream,
            stableToken: String,
            leaseToken: String,
            stagingRoot: File,
        ): StagedAsset {
            if (!SAFE_TOKEN.matches(stableToken) || !SAFE_LEASE_TOKEN.matches(leaseToken)) {
                throw SecurityException("invalid asset reference")
            }
            val stableDirectory = File(stagingRoot, stableToken)
            val directory = File(stableDirectory, leaseToken)
            if (!directory.mkdirs() && !directory.isDirectory) {
                throw MediaBridgeException("stage_failed")
            }
            val destination = File(directory, "original")
            val temporary = File(directory, ".partial-${UUID.randomUUID()}")
            var total = 0L
            try {
                FileOutputStream(temporary).use { output ->
                    val buffer = ByteArray(HASH_BUFFER_BYTES)
                    while (true) {
                        val read = source.read(buffer)
                        if (read < 0) break
                        output.write(buffer, 0, read)
                        total += read
                        if (total > MAXIMUM_ASSET_BYTES) throw AssetTooLarge(total)
                    }
                    output.fd.sync()
                }
                java.nio.file.Files.move(
                    temporary.toPath(),
                    destination.toPath(),
                    java.nio.file.StandardCopyOption.ATOMIC_MOVE,
                    java.nio.file.StandardCopyOption.REPLACE_EXISTING,
                )
                return StagedAsset("$stableToken/$leaseToken/original", total)
            } catch (error: Exception) {
                directory.deleteRecursively()
                if (stableDirectory.list()?.isEmpty() == true) stableDirectory.delete()
                throw error
            } finally {
                temporary.delete()
            }
        }

        @JvmStatic
        internal fun releaseStagedFile(id: String, locator: String, stagingRoot: File) {
            val file = validatedStagedFile(id, locator, stagingRoot)
            val directory = file.parentFile ?: throw SecurityException("invalid asset reference")
            if (!directory.exists()) return
            val canonicalRoot = stagingRoot.canonicalFile
            val stableDirectory = directory.parentFile?.canonicalFile
                ?: throw SecurityException("invalid asset reference")
            if (stableDirectory.parentFile != canonicalRoot) {
                throw SecurityException("invalid asset reference")
            }
            if (!directory.deleteRecursively()) throw MediaBridgeException("release_failed")
            if (stableDirectory.list()?.isEmpty() == true) stableDirectory.delete()
        }

        @JvmStatic
        internal fun validatedStagedFile(id: String, locator: String, stagingRoot: File): File {
            val pieces = locator.split('/')
            if (
                pieces.size != 3 ||
                pieces[2] != "original" ||
                !SAFE_TOKEN.matches(pieces[0]) ||
                !SAFE_LEASE_TOKEN.matches(pieces[1]) ||
                id != "android:${pieces[0]}"
            ) {
                throw SecurityException("invalid asset reference")
            }
            val canonicalRoot = stagingRoot.canonicalFile
            val stableDirectory = File(canonicalRoot, pieces[0]).canonicalFile
            if (stableDirectory.parentFile != canonicalRoot) {
                throw SecurityException("invalid asset reference")
            }
            val directory = File(stableDirectory, pieces[1]).canonicalFile
            if (directory.parentFile != stableDirectory) {
                throw SecurityException("invalid asset reference")
            }
            return File(directory, "original")
        }

        @JvmStatic
        internal fun openOwnedAssetStream(
            context: Context,
            id: String,
            accessKind: String,
            locator: String,
        ): InputStream =
            when (accessKind) {
                "stagedFile" ->
                    validatedStagedFile(
                        id,
                        locator,
                        File(context.filesDir, "upload_staging"),
                    ).inputStream()
                "contentUri" -> {
                    val registry = PersistedLeaseRegistry(
                        AtomicFileLeaseStore(File(context.filesDir, "owned_media_refs.properties")),
                    )
                    val rawUri = registry.resolve(id, locator)
                        ?: throw SecurityException("invalid asset reference")
                    context.contentResolver.openInputStream(Uri.parse(rawUri))
                        ?: throw FileNotFoundException("asset source is unavailable")
                }
                else -> throw SecurityException("invalid asset reference")
            }

        private fun digestText(value: String): String =
            MessageDigest.getInstance("SHA-256")
                .digest(value.toByteArray(Charsets.UTF_8))
                .joinToString("") { "%02x".format(it) }

        private fun safeDisplayName(value: String?): String {
            val normalized = value?.trim().orEmpty()
            return if (normalized.isEmpty()) "照片" else normalized.take(255)
        }
    }
}

internal data class DurableAccess(val kind: String, val locator: String)

private data class PendingPick(
    val reply: ReplyOnce,
    val limit: Int,
    val coordinator: PendingSelectionCoordinator,
    val processing: AtomicBoolean = AtomicBoolean(false),
)

private data class AssetMetadata(val displayName: String?, val size: Long?)
internal data class AssetRef(val id: String, val accessKind: String, val locator: String)
internal data class StagedAsset(val locator: String, val byteSize: Long)

private sealed interface ItemOutcome {
    data class Selected(val value: Map<String, Any>, val ref: AssetRef) : ItemOutcome
    data class Unsupported(val value: Map<String, Any?>) : ItemOutcome
}

internal class PendingSelectionCoordinator(private val cleanup: (AssetRef) -> Unit) {
    private val acquired = mutableListOf<AssetRef>()
    private var cancelled = false
    private var transferred = false

    fun acquire(ref: AssetRef): Boolean {
        val accepted = synchronized(this) {
            if (cancelled || transferred) false
            else {
                acquired += ref
                true
            }
        }
        if (!accepted) cleanup(ref)
        return accepted
    }

    fun dispose(scheduleCleanup: (List<AssetRef>) -> Unit) {
        val abandoned = synchronized(this) {
            if (transferred) emptyList()
            else {
                cancelled = true
                acquired.toList().also { acquired.clear() }
            }
        }
        if (abandoned.isNotEmpty()) scheduleCleanup(abandoned)
    }

    fun transferToFlutter(success: () -> Unit): Boolean {
        val transferredNow = synchronized(this) {
            if (cancelled || transferred) false
            else {
                transferred = true
                acquired.clear()
                true
            }
        }
        if (transferredNow) success()
        return transferredNow
    }
}

internal interface LeaseStore {
    fun read(key: String): String?

    fun entries(): Map<String, String>

    fun update(puts: Map<String, String>, removals: Set<String>)
}

internal class AtomicFileLeaseStore(
    file: File,
) : LeaseStore {
    private val atomicFile = AtomicFile(file)

    override fun read(key: String): String? = load()[key]

    override fun entries(): Map<String, String> = load()

    override fun update(puts: Map<String, String>, removals: Set<String>) {
        val values = load()
        removals.forEach(values::remove)
        values.putAll(puts)
        var output: FileOutputStream? = null
        try {
            val activeOutput = atomicFile.startWrite()
            output = activeOutput
            Properties().apply { putAll(values) }.store(activeOutput, null)
            atomicFile.finishWrite(activeOutput)
            output = null
        } catch (error: Exception) {
            output?.let(atomicFile::failWrite)
            throw MediaBridgeException("lease_store_failed")
        }
    }

    private fun load(): MutableMap<String, String> =
        try {
            atomicFile.openRead().use { input ->
                Properties().apply { load(input) }
                    .entries
                    .associate { (key, value) -> key.toString() to value.toString() }
                    .toMutableMap()
            }
        } catch (_: FileNotFoundException) {
            mutableMapOf()
        } catch (_: Exception) {
            throw MediaBridgeException("lease_store_failed")
        }
}

internal class PersistedLeaseRegistry(private val store: LeaseStore) {
    private data class ActiveLease(val token: String, val id: String, val rawUri: String)
    private data class PendingAcquire(val token: String, val id: String, val rawUri: String)
    private data class GrantRecord(val digest: String, val rawUri: String, val count: Int)

    private data class LedgerSnapshot(
        val activeByToken: Map<String, ActiveLease>,
        val activeByUri: Map<String, List<ActiveLease>>,
        val pendingByToken: Map<String, PendingAcquire>,
        val pendingByUri: Map<String, List<PendingAcquire>>,
        val releasingByToken: Map<String, String>,
        val releasingByUri: Map<String, List<String>>,
        val grantsByUri: Map<String, GrantRecord>,
    ) {
        fun isQuarantined(token: String, rawUri: String): Boolean =
            pendingByToken.containsKey(token) ||
                pendingByUri.containsKey(rawUri) ||
                releasingByToken.containsKey(token) ||
                releasingByUri.containsKey(rawUri)

        fun isRecoverableRelease(token: String, rawUri: String): Boolean =
            releasingByToken[token] == rawUri &&
                releasingByUri[rawUri]?.singleOrNull() == token &&
                activeByToken[token]?.rawUri == rawUri &&
                activeByUri[rawUri]?.singleOrNull()?.token == token &&
                !pendingByToken.containsKey(token) &&
                !pendingByUri.containsKey(rawUri)
    }

    fun acquire(
        id: String,
        rawUri: String,
        leaseToken: String,
        acquireGrant: () -> Boolean,
        rollbackGrant: () -> Unit,
    ): String? = synchronized(TRANSACTION_LOCK) {
        acquireLocked(id, rawUri, leaseToken, acquireGrant, rollbackGrant)
    }

    private fun acquireLocked(
        id: String,
        rawUri: String,
        leaseToken: String,
        acquireGrant: () -> Boolean,
        rollbackGrant: () -> Unit,
    ): String? {
        if (!SAFE_ID.matches(id) || !SAFE_LEASE_TOKEN.matches(leaseToken)) return null
        if (!rawUri.startsWith("content://")) return null
        if (id != expectedId(rawUri)) return null
        val snapshot = ledgerSnapshot() ?: return null
        if (snapshot.isQuarantined(leaseToken, rawUri)) return null
        if (snapshot.activeByToken.containsKey(leaseToken)) return null
        val digest = digest(rawUri)
        val countKey = grantCountKey(digest)
        val uriKey = grantUriKey(digest)
        val count = snapshot.activeByUri[rawUri]?.size ?: 0
        val firstOwner = count == 0
        val locator = leaseLocator(leaseToken)
        if (firstOwner) {
            try {
                store.update(
                    puts = mapOf(
                        pendingIdKey(leaseToken) to id,
                        pendingUriKey(leaseToken) to rawUri,
                    ),
                    removals = emptySet(),
                )
            } catch (_: Exception) {
                return null
            }
            if (!acquireGrant()) {
                runCatching { removePending(leaseToken) }
                return null
            }
        }
        try {
            store.update(
                puts = mapOf(
                    leaseIdKey(leaseToken) to id,
                    leaseUriKey(leaseToken) to rawUri,
                    countKey to (count + 1).toString(),
                    uriKey to rawUri,
                ),
                removals = if (firstOwner) pendingKeys(leaseToken) else emptySet(),
            )
        } catch (_: Exception) {
            if (firstOwner) {
                val rolledBack = try {
                    rollbackGrant()
                    true
                } catch (_: Exception) {
                    false
                }
                if (rolledBack) runCatching { removePending(leaseToken) }
            }
            return null
        }
        return locator
    }

    fun resolve(id: String, locator: String): String? = synchronized(TRANSACTION_LOCK) {
        if (!SAFE_ID.matches(id)) return null
        val leaseToken = leaseToken(locator) ?: return null
        val snapshot = ledgerSnapshot() ?: return null
        val active = snapshot.activeByToken[leaseToken] ?: return null
        if (active.id != id || snapshot.isQuarantined(leaseToken, active.rawUri)) return null
        active.rawUri
    }

    fun reconcilePending(releaseGrant: (String) -> Unit) = synchronized(TRANSACTION_LOCK) {
        reconcileReleasing(releaseGrant)
        val failedTokens = mutableSetOf<String>()
        while (true) {
            val snapshot = ledgerSnapshot() ?: return@synchronized
            val pending = snapshot.pendingByToken.values
                .sortedBy(PendingAcquire::token)
                .firstOrNull { candidate ->
                    candidate.token !in failedTokens &&
                        snapshot.pendingByUri[candidate.rawUri]?.size == 1 &&
                        !snapshot.activeByToken.containsKey(candidate.token) &&
                        !snapshot.releasingByToken.containsKey(candidate.token) &&
                        !snapshot.releasingByUri.containsKey(candidate.rawUri)
                } ?: return@synchronized
            val activeUsesGrant = snapshot.activeByUri[pending.rawUri].orEmpty().isNotEmpty()
            try {
                if (!activeUsesGrant) releaseGrant(pending.rawUri)
                removePending(pending.token)
            } catch (_: Exception) {
                failedTokens += pending.token
            }
        }
    }

    private fun reconcileReleasing(releaseGrant: (String) -> Unit) {
        val failedTokens = mutableSetOf<String>()
        while (true) {
            val snapshot = ledgerSnapshot() ?: return
            val release = snapshot.releasingByToken.entries
                .sortedBy(Map.Entry<String, String>::key)
                .firstOrNull { (token, rawUri) ->
                    token !in failedTokens && snapshot.isRecoverableRelease(token, rawUri)
                } ?: return
            val token = release.key
            val rawUri = release.value
            try {
                releaseGrant(rawUri)
                store.update(
                    puts = emptyMap(),
                    removals = lastOwnerKeys(token, rawUri),
                )
            } catch (_: Exception) {
                failedTokens += token
            }
        }
    }

    fun release(id: String, locator: String, releaseGrant: (String) -> Unit) =
        synchronized(TRANSACTION_LOCK) {
            releaseLocked(id, locator, releaseGrant)
        }

    private fun releaseLocked(id: String, locator: String, releaseGrant: (String) -> Unit) {
        if (!SAFE_ID.matches(id)) throw SecurityException("invalid asset reference")
        val leaseToken = leaseToken(locator)
            ?: throw SecurityException("invalid asset reference")
        val snapshot = ledgerSnapshot()
            ?: throw SecurityException("invalid asset reference")
        val active = snapshot.activeByToken[leaseToken]
        if (active == null) {
            if (
                snapshot.pendingByToken.containsKey(leaseToken) ||
                snapshot.releasingByToken.containsKey(leaseToken)
            ) throw SecurityException("invalid asset reference")
            return
        }
        val rawUri = active.rawUri
        val recoveringRelease = snapshot.isRecoverableRelease(leaseToken, rawUri)
        if (
            active.id != id ||
            (snapshot.isQuarantined(leaseToken, rawUri) && !recoveringRelease)
        ) {
            throw SecurityException("invalid asset reference")
        }
        val digest = digest(rawUri)
        val countKey = grantCountKey(digest)
        val count = snapshot.activeByUri[rawUri]?.size
            ?: throw SecurityException("invalid asset reference")
        val lastOwner = count == 1
        if (!lastOwner) {
            store.update(
                puts = mapOf(countKey to (count - 1).toString()),
                removals = setOf(leaseIdKey(leaseToken), leaseUriKey(leaseToken)),
            )
            return
        }
        if (!recoveringRelease) {
            store.update(
                puts = mapOf(releasingUriKey(leaseToken) to rawUri),
                removals = emptySet(),
            )
        }
        releaseGrant(rawUri)
        store.update(
            puts = emptyMap(),
            removals = lastOwnerKeys(leaseToken, rawUri),
        )
    }

    private fun ledgerSnapshot(): LedgerSnapshot? {
        val entries = try {
            store.entries()
        } catch (_: Exception) {
            return null
        }
        if (entries.keys.any { key ->
                LEASE_KEY.matchEntire(key) == null &&
                    PENDING_KEY.matchEntire(key) == null &&
                    RELEASING_KEY.matchEntire(key) == null &&
                    GRANT_KEY.matchEntire(key) == null
            }
        ) return null

        val activeByToken = mutableMapOf<String, ActiveLease>()
        val activeTokens = entries.keys.mapNotNull { key ->
            LEASE_KEY.matchEntire(key)?.groupValues?.get(1)
        }.toSet()
        for (token in activeTokens) {
            val id = entries[leaseIdKey(token)] ?: return null
            val rawUri = entries[leaseUriKey(token)] ?: return null
            if (
                !SAFE_LEASE_TOKEN.matches(token) ||
                !SAFE_ID.matches(id) ||
                !rawUri.startsWith("content://") ||
                id != expectedId(rawUri)
            ) return null
            activeByToken[token] = ActiveLease(token, id, rawUri)
        }

        val pendingByToken = mutableMapOf<String, PendingAcquire>()
        val pendingTokens = entries.keys.mapNotNull { key ->
            PENDING_KEY.matchEntire(key)?.groupValues?.get(1)
        }.toSet()
        for (token in pendingTokens) {
            val id = entries[pendingIdKey(token)] ?: return null
            val rawUri = entries[pendingUriKey(token)] ?: return null
            if (
                !SAFE_LEASE_TOKEN.matches(token) ||
                !SAFE_ID.matches(id) ||
                !rawUri.startsWith("content://") ||
                id != expectedId(rawUri)
            ) return null
            pendingByToken[token] = PendingAcquire(token, id, rawUri)
        }

        val grantsByUri = mutableMapOf<String, GrantRecord>()
        val grantDigests = entries.keys.mapNotNull { key ->
            GRANT_KEY.matchEntire(key)?.groupValues?.get(1)
        }.toSet()
        for (grantDigest in grantDigests) {
            val count = entries[grantCountKey(grantDigest)]?.toIntOrNull() ?: return null
            val rawUri = entries[grantUriKey(grantDigest)] ?: return null
            if (
                count <= 0 ||
                !rawUri.startsWith("content://") ||
                digest(rawUri) != grantDigest ||
                grantsByUri.containsKey(rawUri)
            ) return null
            grantsByUri[rawUri] = GrantRecord(grantDigest, rawUri, count)
        }

        val activeByUri = activeByToken.values.groupBy(ActiveLease::rawUri)
        if (activeByUri.keys != grantsByUri.keys) return null
        if (activeByUri.any { (rawUri, leases) ->
                grantsByUri[rawUri]?.count != leases.size
            }
        ) return null

        val releasingByToken = mutableMapOf<String, String>()
        for ((key, rawUri) in entries) {
            val match = RELEASING_KEY.matchEntire(key) ?: continue
            val token = match.groupValues[1]
            if (!SAFE_LEASE_TOKEN.matches(token) || !rawUri.startsWith("content://")) return null
            releasingByToken[token] = rawUri
        }
        return LedgerSnapshot(
            activeByToken = activeByToken,
            activeByUri = activeByUri,
            pendingByToken = pendingByToken,
            pendingByUri = pendingByToken.values.groupBy(PendingAcquire::rawUri),
            releasingByToken = releasingByToken,
            releasingByUri = releasingByToken.entries.groupBy(
                keySelector = { it.value },
                valueTransform = { it.key },
            ),
            grantsByUri = grantsByUri,
        )
    }

    private fun leaseToken(locator: String): String? {
        val token = LEASE_LOCATOR.matchEntire(locator)?.groupValues?.get(1)
        return token?.takeIf(SAFE_LEASE_TOKEN::matches)
    }

    private fun leaseLocator(token: String): String =
        "content://com.example.photo_organizer.media-lease/$token"

    private fun leaseIdKey(token: String) = "lease.$token.id"
    private fun leaseUriKey(token: String) = "lease.$token.uri"
    private fun pendingIdKey(token: String) = "pending.$token.id"
    private fun pendingUriKey(token: String) = "pending.$token.uri"
    private fun pendingKeys(token: String) = setOf(pendingIdKey(token), pendingUriKey(token))
    private fun removePending(token: String) = store.update(emptyMap(), pendingKeys(token))
    private fun releasingUriKey(token: String) = "releasing.$token.uri"
    private fun lastOwnerKeys(token: String, rawUri: String): Set<String> {
        val digest = digest(rawUri)
        return setOf(
            leaseIdKey(token),
            leaseUriKey(token),
            grantCountKey(digest),
            grantUriKey(digest),
            releasingUriKey(token),
        )
    }
    private fun grantCountKey(digest: String) = "grant.$digest.count"
    private fun grantUriKey(digest: String) = "grant.$digest.uri"

    private fun digest(value: String): String =
        MessageDigest.getInstance("SHA-256")
            .digest(value.toByteArray(Charsets.UTF_8))
            .joinToString("") { "%02x".format(it) }

    private fun expectedId(rawUri: String): String = "android:${digest(rawUri)}"

    private companion object {
        val TRANSACTION_LOCK = Any()
        val SAFE_ID = Regex("^android:[0-9a-f]{64}$")
        val SAFE_LEASE_TOKEN =
            Regex("^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
        val LEASE_LOCATOR = Regex(
            "^content://com\\.example\\.photo_organizer\\.media-lease/([0-9a-f-]{36})$",
        )
        val LEASE_KEY = Regex("^lease\\.([0-9a-f-]{36})\\.(?:id|uri)$")
        val PENDING_KEY = Regex("^pending\\.([0-9a-f-]{36})\\.(?:id|uri)$")
        val RELEASING_KEY = Regex("^releasing\\.([0-9a-f-]{36})\\.uri$")
        val GRANT_KEY = Regex("^grant\\.([0-9a-f]{64})\\.(?:count|uri)$")
    }
}

private class AssetTooLarge(val byteSize: Long) : Exception()
private class MediaBridgeException(code: String) : Exception(code)

private class ReplyOnce(private val result: MethodChannel.Result) {
    private val completed = AtomicBoolean(false)

    fun success(value: Any?) {
        if (completed.compareAndSet(false, true)) result.success(value)
    }

    fun error(code: String, message: String, details: Any?) {
        if (completed.compareAndSet(false, true)) result.error(code, message, details)
    }

    fun notImplemented() {
        if (completed.compareAndSet(false, true)) result.notImplemented()
    }
}
