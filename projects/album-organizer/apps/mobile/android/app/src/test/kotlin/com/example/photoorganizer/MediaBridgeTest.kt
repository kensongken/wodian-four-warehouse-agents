package com.example.photoorganizer

import java.io.ByteArrayInputStream
import java.io.File
import java.nio.file.Files
import java.security.MessageDigest
import java.util.concurrent.CountDownLatch
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicInteger
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test

class MediaBridgeTest {
    @Test
    fun `normalizes only HEIC HEIF JPEG and PNG MIME types`() {
        assertEquals("image/heic", MediaBridge.normalizedMediaType("image/heic"))
        assertEquals("image/heic", MediaBridge.normalizedMediaType("image/heif"))
        assertEquals("image/jpeg", MediaBridge.normalizedMediaType("image/jpeg"))
        assertEquals("image/png", MediaBridge.normalizedMediaType("image/png"))
        assertNull(MediaBridge.normalizedMediaType("image/x-adobe-dng"))
        assertNull(MediaBridge.normalizedMediaType("video/quicktime"))
    }

    @Test
    fun `negative provider sizes are treated as unknown`() {
        assertNull(MediaBridge.nonNegativeSize(-1))
        assertEquals(0L, MediaBridge.nonNegativeSize(0))
        assertEquals(42L, MediaBridge.nonNegativeSize(42))
    }

    @Test
    fun `picker cap is bounded by remaining task slots and device maximum`() {
        assertEquals(73, MediaBridge.pickerLimit(remainingSlots = 10000, deviceMaximum = 73))
        assertEquals(4, MediaBridge.pickerLimit(remainingSlots = 4, deviceMaximum = 73))
        assertEquals(1, MediaBridge.pickerLimit(remainingSlots = 1, deviceMaximum = 73))
    }

    @Test
    fun `picker remaining slot boundary accepts ten thousand and rejects next value`() {
        assertTrue(MediaBridge.isValidRemainingSlots(10000))
        assertFalse(MediaBridge.isValidRemainingSlots(10001))
    }

    @Test
    fun `framework picker cap uses the public platform maximum`() {
        var maximumCalls = 0

        val limit = MediaBridge.publicPickerLimit(
            remainingSlots = 10000,
            sdkInt = 33,
            rExtensionVersion = 0,
            platformMaximum = {
                maximumCalls += 1
                73
            },
        )

        assertEquals(73, limit)
        assertEquals(1, maximumCalls)
    }

    @Test
    fun `current picker cap reaches the framework platform maximum path`() {
        var maximumCalls = 0

        val limit = MediaBridge.currentPickerLimit(
            remainingSlots = 10000,
            sdkInt = 33,
            rExtensionVersion = 0,
            platformMaximum = {
                maximumCalls += 1
                73
            },
        )

        assertEquals(73, limit)
        assertEquals(1, maximumCalls)
    }

    @Test
    fun `fallback picker cap does not call an unavailable platform API`() {
        var maximumCalls = 0

        val limit = MediaBridge.publicPickerLimit(
            remainingSlots = 10000,
            sdkInt = 29,
            rExtensionVersion = 0,
            platformMaximum = {
                maximumCalls += 1
                error("MediaStore API must not be called")
            },
        )

        assertEquals(10000, limit)
        assertEquals(0, maximumCalls)
    }

    @Test
    fun `failed persistable permission falls back to an owned staged file`() {
        var staged = false

        val access = MediaBridge.chooseDurableAccess(
            persist = { null },
            stage = {
                staged = true
                "${"a".repeat(64)}/4f1ec47b-50f7-4b61-b178-499595afd150/original"
            },
        )

        assertTrue(staged)
        assertEquals("stagedFile", access.kind)
        assertEquals(
            "${"a".repeat(64)}/4f1ec47b-50f7-4b61-b178-499595afd150/original",
            access.locator,
        )
    }

    @Test
    fun `release rejects traversal and does not delete outside app staging`() {
        val sandbox = Files.createTempDirectory("media-bridge-test").toFile()
        val stagingRoot = File(sandbox, "upload_staging").apply { mkdirs() }
        val outside = File(sandbox, "keep.txt").apply { writeText("keep") }

        try {
            assertThrows(SecurityException::class.java) {
                MediaBridge.releaseStagedFile(
                    id = "android:../../keep",
                    locator = "../../keep.txt/original",
                    stagingRoot = stagingRoot,
                )
            }
            assertTrue(outside.exists())
        } finally {
            sandbox.deleteRecursively()
        }
    }

    @Test
    fun `duplicate staged acquisitions use independent leases`() {
        val sandbox = Files.createTempDirectory("media-stage-leases").toFile()
        val root = File(sandbox, "upload_staging")
        val token = "a".repeat(64)
        val id = "android:$token"
        val firstLease = "11111111-1111-4111-8111-111111111111"
        val secondLease = "22222222-2222-4222-8222-222222222222"

        try {
            val first = MediaBridge.stageOwnedStream(
                source = ByteArrayInputStream("first".toByteArray()),
                stableToken = token,
                leaseToken = firstLease,
                stagingRoot = root,
            )
            val second = MediaBridge.stageOwnedStream(
                source = ByteArrayInputStream("second".toByteArray()),
                stableToken = token,
                leaseToken = secondLease,
                stagingRoot = root,
            )

            assertTrue(first.locator != second.locator)
            MediaBridge.releaseStagedFile(id, first.locator, root)
            assertEquals(
                "second",
                MediaBridge.validatedStagedFile(id, second.locator, root).readText(),
            )
            MediaBridge.releaseStagedFile(id, second.locator, root)
            assertTrue(!File(root, token).exists())
        } finally {
            sandbox.deleteRecursively()
        }
    }

    @Test
    fun `persisted duplicate leases keep grant until last owner releases`() {
        val store = MemoryLeaseStore()
        val registry = PersistedLeaseRegistry(store)
        val rawUri = "content://media/external/images/42"
        val id = assetId(rawUri)
        var acquireGrantCalls = 0
        var releaseGrantCalls = 0

        val first = registry.acquire(
            id = id,
            rawUri = rawUri,
            leaseToken = "11111111-1111-4111-8111-111111111111",
            acquireGrant = {
                acquireGrantCalls += 1
                true
            },
            rollbackGrant = { releaseGrantCalls += 1 },
        )
        val second = registry.acquire(
            id = id,
            rawUri = rawUri,
            leaseToken = "22222222-2222-4222-8222-222222222222",
            acquireGrant = {
                acquireGrantCalls += 1
                true
            },
            rollbackGrant = { releaseGrantCalls += 1 },
        )

        assertEquals(1, acquireGrantCalls)
        assertTrue(first != second)
        val restartedRegistry = PersistedLeaseRegistry(store)
        restartedRegistry.release(id, first!!) { releaseGrantCalls += 1 }
        assertEquals(rawUri, restartedRegistry.resolve(id, second!!))
        assertEquals(0, releaseGrantCalls)
        restartedRegistry.release(id, second) { releaseGrantCalls += 1 }
        assertNull(restartedRegistry.resolve(id, second))
        assertEquals(1, releaseGrantCalls)
    }

    @Test
    fun `separate bridge registries serialize the persisted lease transaction`() {
        val store = OverlapDetectingLeaseStore()
        val firstRegistry = PersistedLeaseRegistry(store)
        val recreatedRegistry = PersistedLeaseRegistry(store)
        val pool = Executors.newFixedThreadPool(2)
        val rawUri = "content://media/external/images/77"
        val id = assetId(rawUri)
        val grantCalls = AtomicInteger()

        try {
            val first = pool.submit<String?> {
                firstRegistry.acquire(
                    id = id,
                    rawUri = rawUri,
                    leaseToken = "11111111-1111-4111-8111-111111111111",
                    acquireGrant = {
                        grantCalls.incrementAndGet()
                        true
                    },
                    rollbackGrant = {},
                )
            }
            assertTrue(store.firstRead.await(2, TimeUnit.SECONDS))
            val second = pool.submit<String?> {
                recreatedRegistry.acquire(
                    id = id,
                    rawUri = rawUri,
                    leaseToken = "22222222-2222-4222-8222-222222222222",
                    acquireGrant = {
                        grantCalls.incrementAndGet()
                        true
                    },
                    rollbackGrant = {},
                )
            }

            assertFalse(store.concurrentRead.await(250, TimeUnit.MILLISECONDS))
            store.releaseFirstRead.countDown()
            assertNotNull(first.get(2, TimeUnit.SECONDS))
            assertNotNull(second.get(2, TimeUnit.SECONDS))
            assertEquals(1, grantCalls.get())
        } finally {
            store.releaseFirstRead.countDown()
            pool.shutdownNow()
        }
    }

    @Test
    fun `startup reconciliation retries an interrupted first grant acquisition`() {
        val store = MemoryLeaseStore()
        val registry = PersistedLeaseRegistry(store)
        val token = "33333333-3333-4333-8333-333333333333"
        val rawUri = "content://media/external/images/88"
        store.update(
            puts = mapOf(
                "pending.$token.id" to assetId(rawUri),
                "pending.$token.uri" to rawUri,
            ),
            removals = emptySet(),
        )
        var cleanupCalls = 0

        registry.reconcilePending {
            cleanupCalls += 1
            throw IllegalStateException("provider temporarily unavailable")
        }
        assertEquals(2, store.entries().size)

        registry.reconcilePending { cleanupCalls += 1 }
        assertTrue(store.entries().isEmpty())
        assertEquals(2, cleanupCalls)
    }

    @Test
    fun `stale pending blocks reacquire until its journal is reconciled`() {
        val store = FailActivationAndPendingCleanupStore()
        val registry = PersistedLeaseRegistry(store)
        val rawUri = "content://media/external/images/99"
        val id = assetId(rawUri)
        var rollbackCalls = 0

        val interrupted = registry.acquire(
            id = id,
            rawUri = rawUri,
            leaseToken = "11111111-1111-4111-8111-111111111111",
            acquireGrant = { true },
            rollbackGrant = { rollbackCalls += 1 },
        )
        val blocked = registry.acquire(
            id = id,
            rawUri = rawUri,
            leaseToken = "22222222-2222-4222-8222-222222222222",
            acquireGrant = { true },
            rollbackGrant = { rollbackCalls += 1 },
        )
        var reconcileGrantReleases = 0
        PersistedLeaseRegistry(store).reconcilePending { reconcileGrantReleases += 1 }
        val active = registry.acquire(
            id = id,
            rawUri = rawUri,
            leaseToken = "33333333-3333-4333-8333-333333333333",
            acquireGrant = { true },
            rollbackGrant = { rollbackCalls += 1 },
        )

        assertNull(interrupted)
        assertNull(blocked)
        assertNotNull(active)
        assertEquals(1, rollbackCalls)
        assertEquals(1, reconcileGrantReleases)
        assertEquals(rawUri, PersistedLeaseRegistry(store).resolve(id, active!!))
    }

    @Test
    fun `failed last owner release journal leaves grant and reference untouched`() {
        val store = FailingUpdatesLeaseStore()
        val registry = PersistedLeaseRegistry(store)
        val rawUri = "content://media/external/images/101"
        val id = assetId(rawUri)
        val locator = registry.acquire(
            id = id,
            rawUri = rawUri,
            leaseToken = "11111111-1111-4111-8111-111111111111",
            acquireGrant = { true },
            rollbackGrant = {},
        )!!
        store.failOnUpdate = 3
        var releaseGrantCalls = 0

        assertThrows(IllegalStateException::class.java) {
            registry.release(id, locator) { releaseGrantCalls += 1 }
        }

        assertEquals(0, releaseGrantCalls)
        assertEquals(rawUri, registry.resolve(id, locator))
    }

    @Test
    fun `startup completes interrupted last owner release before same uri reacquires`() {
        val store = FailingUpdatesLeaseStore()
        val registry = PersistedLeaseRegistry(store)
        val rawUri = "content://media/external/images/102"
        val id = assetId(rawUri)
        val locator = registry.acquire(
            id = id,
            rawUri = rawUri,
            leaseToken = "11111111-1111-4111-8111-111111111111",
            acquireGrant = { true },
            rollbackGrant = {},
        )!!
        store.failOnUpdate = 4
        var releaseGrantCalls = 0

        assertThrows(IllegalStateException::class.java) {
            registry.release(id, locator) { releaseGrantCalls += 1 }
        }
        assertEquals(1, releaseGrantCalls)

        var acquireGrantCalls = 0
        val whileReleasing = registry.acquire(
            id = id,
            rawUri = rawUri,
            leaseToken = "22222222-2222-4222-8222-222222222222",
            acquireGrant = {
                acquireGrantCalls += 1
                true
            },
            rollbackGrant = {},
        )
        assertNull(whileReleasing)
        assertEquals(0, acquireGrantCalls)

        store.failOnUpdate = null
        val restarted = PersistedLeaseRegistry(store)
        restarted.reconcilePending { releaseGrantCalls += 1 }
        assertEquals(2, releaseGrantCalls)
        assertNull(restarted.resolve(id, locator))

        val reacquired = restarted.acquire(
            id = id,
            rawUri = rawUri,
            leaseToken = "33333333-3333-4333-8333-333333333333",
            acquireGrant = {
                acquireGrantCalls += 1
                true
            },
            rollbackGrant = {},
        )
        assertNotNull(reacquired)
        assertEquals(1, acquireGrantCalls)
    }

    @Test
    fun `matching release journal can be retried without recreating the registry`() {
        val store = FailingUpdatesLeaseStore()
        val registry = PersistedLeaseRegistry(store)
        val rawUri = "content://media/external/images/104"
        val id = assetId(rawUri)
        val locator = registry.acquire(
            id = id,
            rawUri = rawUri,
            leaseToken = "11111111-1111-4111-8111-111111111111",
            acquireGrant = { true },
            rollbackGrant = {},
        )!!
        store.failOnUpdate = 4
        var releaseGrantCalls = 0
        assertThrows(IllegalStateException::class.java) {
            registry.release(id, locator) { releaseGrantCalls += 1 }
        }
        store.failOnUpdate = null

        registry.release(id, locator) { releaseGrantCalls += 1 }

        assertEquals(2, releaseGrantCalls)
        assertTrue(store.entries().isEmpty())
        assertNull(registry.resolve(id, locator))
    }

    @Test
    fun `corrupt release journal remains quarantined and blocks grant reuse`() {
        val store = MemoryLeaseStore()
        val registry = PersistedLeaseRegistry(store)
        val token = "11111111-1111-4111-8111-111111111111"
        val rawUri = "content://media/external/images/103"
        val activeUri = "content://media/external/images/corrupt"
        val id = assetId(activeUri)
        store.update(
            puts = activeLease(token, id, activeUri) +
                grant(activeUri, count = 1) +
                mapOf("releasing.$token.uri" to rawUri),
            removals = emptySet(),
        )
        var releaseGrantCalls = 0

        registry.reconcilePending { releaseGrantCalls += 1 }

        assertEquals(0, releaseGrantCalls)
        assertEquals(rawUri, store.read("releasing.$token.uri"))
        var acquireGrantCalls = 0
        val reacquired = registry.acquire(
            id = assetId(rawUri),
            rawUri = rawUri,
            leaseToken = "22222222-2222-4222-8222-222222222222",
            acquireGrant = {
                acquireGrantCalls += 1
                true
            },
            rollbackGrant = {},
        )
        assertNull(reacquired)
        assertEquals(0, acquireGrantCalls)
    }

    @Test
    fun `resolve rejects grant count that differs from active lease cardinality`() {
        val store = MemoryLeaseStore()
        val registry = PersistedLeaseRegistry(store)
        val token = "11111111-1111-4111-8111-111111111111"
        val rawUri = "content://media/external/images/201"
        val id = assetId(rawUri)
        store.update(
            puts = activeLease(token, id, rawUri) + grant(rawUri, count = 2),
            removals = emptySet(),
        )

        assertNull(registry.resolve(id, leaseLocator(token)))
    }

    @Test
    fun `acquire rejects orphan grant without taking or mutating ownership`() {
        val store = MemoryLeaseStore()
        val registry = PersistedLeaseRegistry(store)
        val rawUri = "content://media/external/images/202"
        val id = assetId(rawUri)
        store.update(puts = grant(rawUri, count = 1), removals = emptySet())
        val before = store.entries()
        var acquireGrantCalls = 0

        val locator = registry.acquire(
            id = id,
            rawUri = rawUri,
            leaseToken = "22222222-2222-4222-8222-222222222222",
            acquireGrant = {
                acquireGrantCalls += 1
                true
            },
            rollbackGrant = {},
        )

        assertNull(locator)
        assertEquals(0, acquireGrantCalls)
        assertEquals(before, store.entries())
    }

    @Test
    fun `release rejects miscounted ledger without dropping grant or mutating keys`() {
        val store = MemoryLeaseStore()
        val registry = PersistedLeaseRegistry(store)
        val token = "33333333-3333-4333-8333-333333333333"
        val rawUri = "content://media/external/images/203"
        val id = assetId(rawUri)
        store.update(
            puts = activeLease(token, id, rawUri) + grant(rawUri, count = 2),
            removals = emptySet(),
        )
        val before = store.entries()
        var releaseGrantCalls = 0

        assertThrows(SecurityException::class.java) {
            registry.release(id, leaseLocator(token)) { releaseGrantCalls += 1 }
        }

        assertEquals(0, releaseGrantCalls)
        assertEquals(before, store.entries())
    }

    @Test
    fun `pending journal quarantines every active operation for the same uri`() {
        val store = MemoryLeaseStore()
        val registry = PersistedLeaseRegistry(store)
        val activeToken = "11111111-1111-4111-8111-111111111111"
        val pendingToken = "22222222-2222-4222-8222-222222222222"
        val rawUri = "content://media/external/images/204"
        val id = assetId(rawUri)
        store.update(
            puts = activeLease(activeToken, id, rawUri) +
                grant(rawUri, count = 1) +
                mapOf(
                    "pending.$pendingToken.id" to id,
                    "pending.$pendingToken.uri" to rawUri,
                ),
            removals = emptySet(),
        )
        val before = store.entries()
        var acquireGrantCalls = 0
        var releaseGrantCalls = 0

        assertNull(registry.resolve(id, leaseLocator(activeToken)))
        assertNull(
            registry.acquire(
                id = id,
                rawUri = rawUri,
                leaseToken = "33333333-3333-4333-8333-333333333333",
                acquireGrant = {
                    acquireGrantCalls += 1
                    true
                },
                rollbackGrant = {},
            ),
        )
        assertThrows(SecurityException::class.java) {
            registry.release(id, leaseLocator(activeToken)) { releaseGrantCalls += 1 }
        }
        assertEquals(0, acquireGrantCalls)
        assertEquals(0, releaseGrantCalls)
        assertEquals(before, store.entries())
    }

    @Test
    fun `pending journal sharing an active token quarantines both uris`() {
        val store = MemoryLeaseStore()
        val registry = PersistedLeaseRegistry(store)
        val token = "11111111-1111-4111-8111-111111111111"
        val activeUri = "content://media/external/images/205"
        val pendingUri = "content://media/external/images/206"
        store.update(
            puts = activeLease(token, assetId(activeUri), activeUri) +
                grant(activeUri, count = 1) +
                mapOf(
                    "pending.$token.id" to assetId(pendingUri),
                    "pending.$token.uri" to pendingUri,
                ),
            removals = emptySet(),
        )

        assertNull(registry.resolve(assetId(activeUri), leaseLocator(token)))
    }

    @Test
    fun `pending reconciliation preserves journal for an orphan grant`() {
        val store = MemoryLeaseStore()
        val registry = PersistedLeaseRegistry(store)
        val token = "11111111-1111-4111-8111-111111111111"
        val rawUri = "content://media/external/images/207"
        store.update(
            puts = grant(rawUri, count = 1) + mapOf(
                "pending.$token.id" to assetId(rawUri),
                "pending.$token.uri" to rawUri,
            ),
            removals = emptySet(),
        )
        val before = store.entries()
        var releaseGrantCalls = 0

        registry.reconcilePending { releaseGrantCalls += 1 }

        assertEquals(0, releaseGrantCalls)
        assertEquals(before, store.entries())
    }

    @Test
    fun `failed pending recovery does not starve an independent journal`() {
        val store = MemoryLeaseStore()
        val registry = PersistedLeaseRegistry(store)
        val firstToken = "11111111-1111-4111-8111-111111111111"
        val secondToken = "22222222-2222-4222-8222-222222222222"
        val firstUri = "content://media/external/images/210"
        val secondUri = "content://media/external/images/211"
        store.update(
            puts = mapOf(
                "pending.$firstToken.id" to assetId(firstUri),
                "pending.$firstToken.uri" to firstUri,
                "pending.$secondToken.id" to assetId(secondUri),
                "pending.$secondToken.uri" to secondUri,
            ),
            removals = emptySet(),
        )
        val releaseAttempts = mutableListOf<String>()

        registry.reconcilePending { rawUri ->
            releaseAttempts += rawUri
            if (rawUri == firstUri) throw IllegalStateException("provider unavailable")
        }

        assertEquals(listOf(firstUri, secondUri), releaseAttempts)
        assertEquals(firstUri, store.read("pending.$firstToken.uri"))
        assertNull(store.read("pending.$secondToken.uri"))
    }

    @Test
    fun `release reconciliation refuses count one when two active leases exist`() {
        val store = MemoryLeaseStore()
        val registry = PersistedLeaseRegistry(store)
        val firstToken = "11111111-1111-4111-8111-111111111111"
        val secondToken = "22222222-2222-4222-8222-222222222222"
        val rawUri = "content://media/external/images/208"
        val id = assetId(rawUri)
        store.update(
            puts = activeLease(firstToken, id, rawUri) +
                activeLease(secondToken, id, rawUri) +
                grant(rawUri, count = 1) +
                mapOf("releasing.$firstToken.uri" to rawUri),
            removals = emptySet(),
        )
        val before = store.entries()
        var releaseGrantCalls = 0

        registry.reconcilePending { releaseGrantCalls += 1 }

        assertEquals(0, releaseGrantCalls)
        assertEquals(before, store.entries())
    }

    @Test
    fun `failed release recovery does not starve an independent journal`() {
        val store = MemoryLeaseStore()
        val registry = PersistedLeaseRegistry(store)
        val firstToken = "11111111-1111-4111-8111-111111111111"
        val secondToken = "22222222-2222-4222-8222-222222222222"
        val firstUri = "content://media/external/images/212"
        val secondUri = "content://media/external/images/213"
        store.update(
            puts = activeLease(firstToken, assetId(firstUri), firstUri) +
                grant(firstUri, count = 1) +
                activeLease(secondToken, assetId(secondUri), secondUri) +
                grant(secondUri, count = 1) +
                mapOf(
                    "releasing.$firstToken.uri" to firstUri,
                    "releasing.$secondToken.uri" to secondUri,
                ),
            removals = emptySet(),
        )
        val releaseAttempts = mutableListOf<String>()

        registry.reconcilePending { rawUri ->
            releaseAttempts += rawUri
            if (rawUri == firstUri) throw IllegalStateException("provider unavailable")
        }

        assertEquals(listOf(firstUri, secondUri), releaseAttempts)
        assertEquals(firstUri, store.read("releasing.$firstToken.uri"))
        assertNull(store.read("releasing.$secondToken.uri"))
        assertNotNull(store.read("lease.$firstToken.id"))
        assertNull(store.read("lease.$secondToken.id"))
    }

    @Test
    fun `stable id mismatch quarantines an otherwise complete lease`() {
        val store = MemoryLeaseStore()
        val registry = PersistedLeaseRegistry(store)
        val token = "11111111-1111-4111-8111-111111111111"
        val rawUri = "content://media/external/images/209"
        val wrongId = "android:${"a".repeat(64)}"
        store.update(
            puts = activeLease(token, wrongId, rawUri) + grant(rawUri, count = 1),
            removals = emptySet(),
        )

        assertNull(registry.resolve(wrongId, leaseLocator(token)))
    }

    @Test
    fun `processing disposal barrier cleans current and late refs without success`() {
        val first = AssetRef(
            id = "android:${"c".repeat(64)}",
            accessKind = "stagedFile",
            locator = "${"c".repeat(64)}/11111111-1111-4111-8111-111111111111/original",
        )
        val late = AssetRef(
            id = first.id,
            accessKind = "stagedFile",
            locator = "${"c".repeat(64)}/22222222-2222-4222-8222-222222222222/original",
        )
        val cleaned = mutableListOf<AssetRef>()
        val coordinator = PendingSelectionCoordinator(cleaned::add)
        val firstAcquired = CountDownLatch(1)
        val finishLateStage = CountDownLatch(1)
        val worker = Executors.newSingleThreadExecutor()
        var successReplies = 0

        try {
            val processing = worker.submit {
                assertTrue(coordinator.acquire(first))
                firstAcquired.countDown()
                finishLateStage.await(2, TimeUnit.SECONDS)
                assertTrue(!coordinator.acquire(late))
                coordinator.transferToFlutter { successReplies += 1 }
            }
            assertTrue(firstAcquired.await(2, TimeUnit.SECONDS))
            coordinator.dispose { abandoned -> abandoned.forEach(cleaned::add) }
            finishLateStage.countDown()
            processing.get(2, TimeUnit.SECONDS)
        } finally {
            finishLateStage.countDown()
            worker.shutdownNow()
        }

        assertEquals(listOf(first, late), cleaned)
        assertEquals(1, cleaned.count { it == first })
        assertEquals(1, cleaned.count { it == late })
        assertEquals(0, successReplies)
        coordinator.dispose { abandoned -> abandoned.forEach(cleaned::add) }
        assertEquals(2, cleaned.size)
    }
}

private class MemoryLeaseStore : LeaseStore {
    private val values = mutableMapOf<String, String>()

    override fun read(key: String): String? = values[key]

    override fun entries(): Map<String, String> = values.toMap()

    override fun update(puts: Map<String, String>, removals: Set<String>) {
        removals.forEach(values::remove)
        values.putAll(puts)
    }
}

private class OverlapDetectingLeaseStore : LeaseStore {
    private val values = mutableMapOf<String, String>()
    private val activeReads = AtomicInteger()
    private val shouldHoldFirstRead = AtomicBoolean(true)
    val firstRead = CountDownLatch(1)
    val releaseFirstRead = CountDownLatch(1)
    val concurrentRead = CountDownLatch(1)

    override fun read(key: String): String? = trackedRead { values[key] }

    override fun entries(): Map<String, String> = trackedRead { values.toMap() }

    private fun <T> trackedRead(operation: () -> T): T {
        if (activeReads.incrementAndGet() > 1) concurrentRead.countDown()
        try {
            if (shouldHoldFirstRead.compareAndSet(true, false)) {
                firstRead.countDown()
                releaseFirstRead.await(2, TimeUnit.SECONDS)
            }
            return operation()
        } finally {
            activeReads.decrementAndGet()
        }
    }

    override fun update(puts: Map<String, String>, removals: Set<String>) {
        removals.forEach(values::remove)
        values.putAll(puts)
    }
}

private class FailActivationAndPendingCleanupStore : LeaseStore {
    private val values = mutableMapOf<String, String>()
    private var updates = 0

    override fun read(key: String): String? = values[key]

    override fun entries(): Map<String, String> = values.toMap()

    override fun update(puts: Map<String, String>, removals: Set<String>) {
        updates += 1
        if (updates == 2 || updates == 3) throw IllegalStateException("simulated disk failure")
        removals.forEach(values::remove)
        values.putAll(puts)
    }
}

private class FailingUpdatesLeaseStore : LeaseStore {
    private val values = mutableMapOf<String, String>()
    private var updates = 0
    var failOnUpdate: Int? = null

    override fun read(key: String): String? = values[key]

    override fun entries(): Map<String, String> = values.toMap()

    override fun update(puts: Map<String, String>, removals: Set<String>) {
        updates += 1
        if (updates == failOnUpdate) throw IllegalStateException("simulated disk failure")
        removals.forEach(values::remove)
        values.putAll(puts)
    }
}

private fun activeLease(token: String, id: String, rawUri: String): Map<String, String> = mapOf(
    "lease.$token.id" to id,
    "lease.$token.uri" to rawUri,
)

private fun grant(rawUri: String, count: Int): Map<String, String> {
    val digest = sha256(rawUri)
    return mapOf(
        "grant.$digest.count" to count.toString(),
        "grant.$digest.uri" to rawUri,
    )
}

private fun leaseLocator(token: String): String =
    "content://com.example.photo_organizer.media-lease/$token"

private fun assetId(rawUri: String): String = "android:${sha256(rawUri)}"

private fun sha256(value: String): String =
    MessageDigest.getInstance("SHA-256")
        .digest(value.toByteArray(Charsets.UTF_8))
        .joinToString("") { "%02x".format(it) }
