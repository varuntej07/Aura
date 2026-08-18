package dev.varuntej.aura.keyboard.prediction

import java.util.Collections
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class PredictionCoordinatorTest {
    @Test
    fun newerRequest_cancelsBeforeDebounce_andOnlyLatestIsDelivered() {
        val delivered = Collections.synchronizedList(mutableListOf<String>())
        val latch = CountDownLatch(1)
        val coordinator = coordinator(
            lexicalDelayMs = 60,
            lexical = { request, _ -> request },
            deliver = { _, stage, result ->
                if (stage == PredictionStage.LEXICAL) {
                    delivered.add(result)
                    latch.countDown()
                }
            },
        )
        try {
            coordinator.submit("old")
            coordinator.submit("latest")
            assertTrue(latch.await(2, TimeUnit.SECONDS))
            assertEquals(listOf("latest"), delivered)
        } finally {
            coordinator.close()
        }
    }

    @Test
    fun newerRequest_cooperativelyCancelsActiveLexicalWork() {
        val firstStarted = CountDownLatch(1)
        val firstCancelled = CountDownLatch(1)
        val latestDelivered = CountDownLatch(1)
        val coordinator = coordinator(
            lexicalDelayMs = 0,
            lexical = { request, cancellation ->
                if (request == "old") {
                    firstStarted.countDown()
                    while (!cancellation.isCancelled()) Thread.yield()
                    firstCancelled.countDown()
                    null
                } else {
                    request
                }
            },
            deliver = { _, stage, result ->
                if (stage == PredictionStage.LEXICAL && result == "latest") latestDelivered.countDown()
            },
        )
        try {
            coordinator.submit("old")
            assertTrue(firstStarted.await(1, TimeUnit.SECONDS))
            coordinator.submit("latest")
            assertTrue(firstCancelled.await(1, TimeUnit.SECONDS))
            assertTrue(latestDelivered.await(1, TimeUnit.SECONDS))
        } finally {
            coordinator.close()
        }
    }

    @Test
    fun newerRequest_invokesActiveNativeInferenceTerminationCallback() {
        val inferenceStarted = CountDownLatch(1)
        val nativeTermination = CountDownLatch(1)
        val latestDelivered = CountDownLatch(1)
        val coordinator = coordinator(
            lexicalDelayMs = 0,
            lexical = { request, cancellation ->
                if (request == "inference") {
                    val terminate = { nativeTermination.countDown() }
                    cancellation.installCancellationCallback(terminate)
                    inferenceStarted.countDown()
                    try {
                        nativeTermination.await(1, TimeUnit.SECONDS)
                        null
                    } finally {
                        cancellation.removeCancellationCallback(terminate)
                    }
                } else {
                    request
                }
            },
            deliver = { _, stage, result ->
                if (stage == PredictionStage.LEXICAL && result == "latest") {
                    latestDelivered.countDown()
                }
            },
        )
        try {
            coordinator.submit("inference")
            assertTrue(inferenceStarted.await(1, TimeUnit.SECONDS))
            coordinator.submit("latest")
            assertTrue(nativeTermination.await(1, TimeUnit.SECONDS))
            assertTrue(latestDelivered.await(1, TimeUnit.SECONDS))
        } finally {
            coordinator.close()
        }
    }

    @Test
    fun invalidationRejectsLateResult() {
        val started = CountDownLatch(1)
        val release = CountDownLatch(1)
        val delivered = CountDownLatch(1)
        val coordinator = coordinator(
            lexicalDelayMs = 0,
            lexical = { request, _ ->
                started.countDown()
                release.await(1, TimeUnit.SECONDS)
                request
            },
            deliver = { _, _, _ -> delivered.countDown() },
        )
        try {
            coordinator.submit("stale")
            assertTrue(started.await(1, TimeUnit.SECONDS))
            coordinator.invalidate()
            release.countDown()
            assertFalse(delivered.await(150, TimeUnit.MILLISECONDS))
        } finally {
            release.countDown()
            coordinator.close()
        }
    }

    @Test
    fun rapidInputKeepsOnlyOneLexicalAndOneDeferredTaskQueued() {
        val coordinator = coordinator(
            lexicalDelayMs = 10_000,
            deferredDelayMs = 20_000,
            lexical = { request, _ -> request },
        )
        try {
            repeat(2_000) { coordinator.submit("request-$it") }
            val diagnostics = coordinator.diagnostics()
            assertTrue("queued=${diagnostics.queuedTasks}", diagnostics.queuedTasks <= 2)
            assertTrue(diagnostics.hasPendingLexical)
            assertTrue(diagnostics.hasPendingDeferred)
        } finally {
            coordinator.close()
        }
    }

    @Test
    fun closeReturnsWithoutWaiting_andCleanupRunsOnPredictionWorker() {
        val started = CountDownLatch(1)
        val cleaned = CountDownLatch(1)
        var cleanupThread = ""
        val coordinator = PredictionCoordinator<String, String>(
            lexicalDelayMs = 0,
            deferredDelayMs = 10_000,
            lexicalWork = { _, cancellation ->
                started.countDown()
                while (!cancellation.isCancelled()) Thread.yield()
                null
            },
            deferredWork = { _, _ -> null },
            deliver = { _, _, _ -> },
            workerCleanup = {
                cleanupThread = Thread.currentThread().name
                cleaned.countDown()
            },
        )
        coordinator.submit("active")
        assertTrue(started.await(1, TimeUnit.SECONDS))
        coordinator.close()
        assertTrue(cleaned.await(1, TimeUnit.SECONDS))
        assertEquals("AuraImePrediction", cleanupThread)
    }

    private fun coordinator(
        lexicalDelayMs: Long,
        deferredDelayMs: Long = 10_000,
        lexical: (String, PredictionCancellation) -> String?,
        deliver: (Long, PredictionStage, String) -> Unit = { _, _, _ -> },
    ): PredictionCoordinator<String, String> = PredictionCoordinator(
        lexicalDelayMs = lexicalDelayMs,
        deferredDelayMs = deferredDelayMs,
        lexicalWork = lexical,
        deferredWork = { _, _ -> null },
        deliver = deliver,
    )
}
