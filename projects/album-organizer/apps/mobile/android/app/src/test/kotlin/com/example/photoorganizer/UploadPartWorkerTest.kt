package com.example.photoorganizer

import android.net.NetworkCapabilities
import java.io.ByteArrayInputStream
import java.io.EOFException
import androidx.work.WorkInfo
import kotlinx.coroutines.cancelAndJoin
import kotlinx.coroutines.launch
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.yield
import okhttp3.Call
import okhttp3.Callback
import okhttp3.Request
import okhttp3.Response
import okio.Timeout
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test

class UploadPartWorkerTest {
    @Test
    fun `unique work name deduplicates one stable job without exposing its id`() {
        val first = UploadPartWorker.uniqueWorkName("task:asset:2:7")
        val duplicate = UploadPartWorker.uniqueWorkName("task:asset:2:7")

        assertEquals(first, duplicate)
        assertTrue(first.startsWith("upload-part-"))
        assertFalse(first.contains("task:asset"))
    }

    @Test
    fun `job id validation rejects control characters and oversized values`() {
        assertTrue(BackgroundTransferBridge.safeJobId("task:asset:2:7"))
        assertFalse(BackgroundTransferBridge.safeJobId("task\nasset"))
        assertFalse(BackgroundTransferBridge.safeJobId("x".repeat(513)))
    }

    @Test
    fun `range streaming detects a missing source offset`() {
        assertThrows(EOFException::class.java) {
            RangeRequestBody.skipFully(ByteArrayInputStream(byteArrayOf(1, 2)), 3)
        }
    }

    @Test
    fun `wifi only selects noncellular transports and mobile override selects any network`() {
        assertEquals(
            setOf(
                NetworkCapabilities.TRANSPORT_WIFI,
                NetworkCapabilities.TRANSPORT_ETHERNET,
            ),
            BackgroundTransferBridge.requiredTransports(allowMobileData = false),
        )
        assertEquals(
            emptySet<Int>(),
            BackgroundTransferBridge.requiredTransports(allowMobileData = true),
        )
    }

    @Test
    fun `constraint cancellation retains URL while terminal results remove it`() {
        assertFalse(UploadPartWorker.shouldRemoveSignedUrl(terminalResult = false))
        assertTrue(UploadPartWorker.shouldRemoveSignedUrl(terminalResult = true))
    }

    @Test
    fun `startup query resubscribes only nonterminal native jobs`() {
        assertTrue(BackgroundTransferBridge.shouldObserve(WorkInfo.State.ENQUEUED))
        assertTrue(BackgroundTransferBridge.shouldObserve(WorkInfo.State.BLOCKED))
        assertTrue(BackgroundTransferBridge.shouldObserve(WorkInfo.State.RUNNING))
        assertFalse(BackgroundTransferBridge.shouldObserve(WorkInfo.State.SUCCEEDED))
        assertFalse(BackgroundTransferBridge.shouldObserve(WorkInfo.State.FAILED))
        assertFalse(BackgroundTransferBridge.shouldObserve(WorkInfo.State.CANCELLED))
    }

    @Test
    fun `signed URL expiry is retryable and successful ETag is preserved`() {
        val expired = UploadPartWorker.httpOutcome(403, null)
        val succeeded = UploadPartWorker.httpOutcome(200, " etag-worker ")

        assertEquals("signed_url_expired", expired.code)
        assertTrue(expired.retryable)
        assertEquals("etag-worker", succeeded.etag)
    }

    @Test
    fun `missing or oversized ETag cannot become a successful result`() {
        assertEquals("invalid_transfer_result", UploadPartWorker.httpOutcome(200, null).code)
        assertEquals(
            "invalid_transfer_result",
            UploadPartWorker.httpOutcome(200, "x".repeat(257)).code,
        )
    }

    @Test
    fun `cancelling a worker wait cancels the underlying HTTP call`() = runBlocking {
        val call = NeverCompletingCall()
        val waiting = launch { UploadPartWorker.awaitResponse(call) }
        yield()

        waiting.cancelAndJoin()

        assertTrue(call.isCanceled())
    }
}

private class NeverCompletingCall : Call {
    private var cancelled = false

    override fun request(): Request = Request.Builder()
        .url("https://upload.example.test/part")
        .build()

    override fun execute(): Response = error("execute must not be used")

    override fun enqueue(responseCallback: Callback) {}

    override fun cancel() {
        cancelled = true
    }

    override fun isExecuted(): Boolean = false

    override fun isCanceled(): Boolean = cancelled

    override fun timeout(): Timeout = Timeout.NONE

    override fun clone(): Call = NeverCompletingCall()
}
