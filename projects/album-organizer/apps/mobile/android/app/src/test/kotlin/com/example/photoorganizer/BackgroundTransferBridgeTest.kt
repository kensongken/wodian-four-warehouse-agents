package com.example.photoorganizer

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class BackgroundTransferBridgeTest {
    @Test
    fun `query recovery job id boundary accepts ten thousand and rejects next value`() {
        val accepted = (0 until 10000).map { "task:asset:$it" }
        val rejected = (0 until 10001).map { "task:asset:$it" }

        assertEquals(
            10000,
            BackgroundTransferBridge.validatedQueryJobIds(mapOf("jobIds" to accepted))?.size,
        )
        assertNull(
            BackgroundTransferBridge.validatedQueryJobIds(mapOf("jobIds" to rejected)),
        )
    }
}
