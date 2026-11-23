package com.example.photoorganizer

import android.content.Context
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import androidx.work.CoroutineWorker
import androidx.work.Data
import androidx.work.WorkerParameters
import java.io.EOFException
import java.io.IOException
import java.io.InputStream
import java.security.KeyStore
import java.security.MessageDigest
import java.util.Base64
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec
import kotlin.coroutines.resumeWithException
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.coroutines.sync.Semaphore
import kotlinx.coroutines.sync.withPermit
import kotlinx.coroutines.withContext
import okhttp3.Call
import okhttp3.Callback
import okhttp3.MediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody
import okhttp3.Response
import okio.BufferedSink

class UploadPartWorker(
    appContext: Context,
    parameters: WorkerParameters,
) : CoroutineWorker(appContext, parameters) {
    override suspend fun doWork(): Result = withContext(Dispatchers.IO) {
        val jobId = inputData.getString(KEY_JOB_ID) ?: return@withContext invalidSource()
        val id = inputData.getString(KEY_ASSET_ID) ?: return@withContext invalidSource()
        val accessKind = inputData.getString(KEY_ACCESS_KIND) ?: return@withContext invalidSource()
        val locator = inputData.getString(KEY_LOCATOR) ?: return@withContext invalidSource()
        val offset = inputData.getLong(KEY_OFFSET, -1)
        val length = inputData.getLong(KEY_LENGTH, -1)
        val allowMobileData = inputData.getBoolean(KEY_ALLOW_MOBILE_DATA, false)
        if (offset < 0 || length <= 0) return@withContext invalidSource()
        if (!networkPolicyAllows(applicationContext, allowMobileData)) {
            return@withContext Result.retry()
        }
        val vault = SignedUrlVault(applicationContext)
        val signedUrl = try {
            vault.read(jobId) ?: return@withContext expiredUrl()
        } catch (_: Exception) {
            return@withContext expiredUrl()
        }
        val source = try {
            MediaBridge.openOwnedAssetStream(applicationContext, id, accessKind, locator)
        } catch (_: Exception) {
            vault.remove(jobId)
            return@withContext invalidSource()
        }
        val body = RangeRequestBody(source, offset, length) { progress ->
            setProgressAsync(Data.Builder().putInt(KEY_PROGRESS, progress).build())
        }
        val request = Request.Builder().url(signedUrl).put(body).build()
        var terminalResult = false
        try {
            val result = TRANSFER_PERMITS.withPermit {
                awaitResponse(HTTP_CLIENT.newCall(request)).use {
                    val outcome = httpOutcome(it.code, it.header("ETag"))
                    if (outcome.etag != null) {
                        return@withPermit Result.success(
                            Data.Builder()
                                .putString(KEY_RESULT_STATE, "succeeded")
                                .putString(KEY_ETAG, outcome.etag)
                                .build(),
                        )
                    }
                    return@withPermit failed(outcome.code!!, outcome.retryable)
                }
            }
            terminalResult = true
            result
        } catch (error: CancellationException) {
            throw error
        } catch (_: EOFException) {
            terminalResult = true
            invalidSource()
        } catch (_: SourceReadException) {
            terminalResult = true
            invalidSource()
        } catch (_: Exception) {
            terminalResult = true
            failed("network_unavailable", true)
        } finally {
            try {
                source.close()
            } catch (_: Exception) {
                // Closing a provider stream must not turn a WorkManager stop
                // into a terminal result or delete its resumable credential.
            }
            if (shouldRemoveSignedUrl(terminalResult)) vault.remove(jobId)
        }
    }

    private fun invalidSource(): Result = failed("source_read_failed", false)

    private fun expiredUrl(): Result = failed("signed_url_expired", true)

    private fun failed(code: String, retryable: Boolean): Result =
        Result.failure(
            Data.Builder()
                .putString(KEY_RESULT_STATE, "failed")
                .putString(KEY_CODE, code)
                .putBoolean(KEY_RETRYABLE, retryable)
                .build(),
        )

    companion object {
        internal const val KEY_JOB_ID = "job_id"
        internal const val KEY_ASSET_ID = "asset_id"
        internal const val KEY_ACCESS_KIND = "access_kind"
        internal const val KEY_LOCATOR = "locator"
        internal const val KEY_OFFSET = "byte_offset"
        internal const val KEY_LENGTH = "byte_length"
        internal const val KEY_ALLOW_MOBILE_DATA = "allow_mobile_data"
        internal const val KEY_PROGRESS = "progress"
        internal const val KEY_RESULT_STATE = "result_state"
        internal const val KEY_ETAG = "etag"
        internal const val KEY_CODE = "code"
        internal const val KEY_RETRYABLE = "retryable"

        private val HTTP_CLIENT = OkHttpClient.Builder().retryOnConnectionFailure(false).build()
        private val TRANSFER_PERMITS = Semaphore(3)

        internal fun shouldRemoveSignedUrl(terminalResult: Boolean): Boolean = terminalResult

        internal fun networkPolicyAllows(
            context: Context,
            allowMobileData: Boolean,
        ): Boolean {
            if (allowMobileData) return true
            val connectivity = context.getSystemService(ConnectivityManager::class.java)
            val capabilities = connectivity.getNetworkCapabilities(connectivity.activeNetwork)
                ?: return false
            return capabilities.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) ||
                capabilities.hasTransport(NetworkCapabilities.TRANSPORT_ETHERNET)
        }

        @OptIn(ExperimentalCoroutinesApi::class)
        internal suspend fun awaitResponse(call: Call): Response =
            suspendCancellableCoroutine { continuation ->
                continuation.invokeOnCancellation { call.cancel() }
                call.enqueue(
                    object : Callback {
                        override fun onFailure(call: Call, e: IOException) {
                            continuation.resumeWithException(e)
                        }

                        override fun onResponse(call: Call, response: Response) {
                            continuation.resume(response) { response.close() }
                        }
                    },
                )
            }

        internal fun uniqueWorkName(jobId: String): String {
            val digest = MessageDigest.getInstance("SHA-256")
                .digest(jobId.toByteArray(Charsets.UTF_8))
                .joinToString("") { "%02x".format(it) }
            return "upload-part-$digest"
        }

        internal fun httpOutcome(statusCode: Int, rawEtag: String?): HttpOutcome {
            if (statusCode == 401 || statusCode == 403) {
                return HttpOutcome(code = "signed_url_expired", retryable = true)
            }
            if (statusCode !in 200..299) {
                return HttpOutcome(
                    code = if (statusCode >= 500) "service_unavailable" else "upload_rejected",
                    retryable = statusCode >= 500,
                )
            }
            val etag = rawEtag?.trim()
            return if (etag.isNullOrEmpty() || etag.length > 256) {
                HttpOutcome(code = "invalid_transfer_result", retryable = true)
            } else {
                HttpOutcome(etag = etag)
            }
        }
    }
}

internal data class HttpOutcome(
    val etag: String? = null,
    val code: String? = null,
    val retryable: Boolean = false,
)

internal class SourceReadException(cause: IOException) : IOException(cause)

internal class RangeRequestBody(
    private val source: InputStream,
    private val offset: Long,
    private val length: Long,
    private val onProgress: (Int) -> Unit,
) : RequestBody() {
    override fun contentType(): MediaType? = null

    override fun contentLength(): Long = length

    override fun writeTo(sink: BufferedSink) {
        try {
            skipFully(source, offset)
        } catch (error: EOFException) {
            throw error
        } catch (error: IOException) {
            throw SourceReadException(error)
        }
        val buffer = ByteArray(DEFAULT_BUFFER_SIZE)
        var remaining = length
        var sent = 0L
        var lastProgress = -1
        while (remaining > 0) {
            val read = try {
                source.read(buffer, 0, minOf(buffer.size.toLong(), remaining).toInt())
            } catch (error: IOException) {
                throw SourceReadException(error)
            }
            if (read < 0) throw EOFException("asset ended before the requested part")
            sink.write(buffer, 0, read)
            remaining -= read
            sent += read
            val progress = (sent * 100 / length).toInt().coerceIn(0, 100)
            if (progress != lastProgress) {
                lastProgress = progress
                onProgress(progress)
            }
        }
    }

    companion object {
        internal fun skipFully(source: InputStream, bytes: Long) {
            var remaining = bytes
            while (remaining > 0) {
                val skipped = source.skip(remaining)
                if (skipped > 0) {
                    remaining -= skipped
                    continue
                }
                if (source.read() < 0) throw EOFException("asset ended before the requested offset")
                remaining -= 1
            }
        }
    }
}

internal class SignedUrlVault(context: Context) {
    private val preferences = context.getSharedPreferences(PREFERENCES, Context.MODE_PRIVATE)

    fun put(jobId: String, signedUrl: String) {
        val cipher = Cipher.getInstance(TRANSFORMATION)
        cipher.init(Cipher.ENCRYPT_MODE, key())
        val encrypted = cipher.doFinal(signedUrl.toByteArray(Charsets.UTF_8))
        val payload = cipher.iv + encrypted
        check(
            preferences.edit()
                .putString(storageKey(jobId), Base64.getEncoder().encodeToString(payload))
                .commit(),
        )
    }

    fun read(jobId: String): String? {
        val encoded = preferences.getString(storageKey(jobId), null) ?: return null
        val payload = Base64.getDecoder().decode(encoded)
        require(payload.size > IV_BYTES)
        val cipher = Cipher.getInstance(TRANSFORMATION)
        cipher.init(
            Cipher.DECRYPT_MODE,
            key(),
            GCMParameterSpec(128, payload.copyOfRange(0, IV_BYTES)),
        )
        return cipher.doFinal(payload.copyOfRange(IV_BYTES, payload.size)).toString(Charsets.UTF_8)
    }

    fun remove(jobId: String): Boolean =
        preferences.edit().remove(storageKey(jobId)).commit()

    private fun key(): SecretKey {
        val store = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        (store.getKey(KEY_ALIAS, null) as? SecretKey)?.let { return it }
        val generator = KeyGenerator.getInstance("AES", "AndroidKeyStore")
        generator.init(
            android.security.keystore.KeyGenParameterSpec.Builder(
                KEY_ALIAS,
                android.security.keystore.KeyProperties.PURPOSE_ENCRYPT or
                    android.security.keystore.KeyProperties.PURPOSE_DECRYPT,
            ).setBlockModes(android.security.keystore.KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(android.security.keystore.KeyProperties.ENCRYPTION_PADDING_NONE)
                .build(),
        )
        return generator.generateKey()
    }

    private fun storageKey(jobId: String): String =
        MessageDigest.getInstance("SHA-256")
            .digest(jobId.toByteArray(Charsets.UTF_8))
            .joinToString("") { "%02x".format(it) }

    companion object {
        private const val PREFERENCES = "background_upload_signed_url_v1"
        private const val KEY_ALIAS = "photo_organizer_background_upload_v1"
        private const val TRANSFORMATION = "AES/GCM/NoPadding"
        private const val IV_BYTES = 12
    }
}
