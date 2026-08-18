package dev.varuntej.aura.keyboard.prediction

import java.io.Closeable
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicLong
import java.util.concurrent.atomic.AtomicReference
import java.util.concurrent.locks.LockSupport

enum class PredictionStage { LEXICAL, DEFERRED }

interface PredictionCoordinatorObserver {
    fun onMailboxPublished() {}
    fun onStageStarted(stage: PredictionStage) {}
    fun onStageFinished(stage: PredictionStage, currentRequestRemainingStages: Int) {}
    fun onInvalidated() {}

    companion object {
        val NONE = object : PredictionCoordinatorObserver {}
    }
}

/**
 * A single parked worker with a lock-free latest-request mailbox.
 *
 * Main-thread publication performs only atomic stores and `unpark`: there is no executor queue,
 * Handler message, Future, monitor, or scheduling lock to contend on. The worker owns debounce
 * timing. A new generation overwrites the mailbox and cooperatively cancels active work; logical
 * pending work is therefore bounded to one lexical and one deferred stage for the newest request.
 */
class PredictionCoordinator<Request : Any, Result : Any>(
    lexicalDelayMs: Long,
    deferredDelayMs: Long,
    threadName: String = "AuraImePrediction",
    private val lexicalWork: (Request, PredictionCancellation) -> Result?,
    private val deferredWork: (Request, PredictionCancellation) -> Result?,
    private val deliver: (generation: Long, stage: PredictionStage, result: Result) -> Unit,
    private val observer: PredictionCoordinatorObserver = PredictionCoordinatorObserver.NONE,
    private val workerCleanup: () -> Unit = {},
) : Closeable {
    private data class Envelope<Request>(
        val generation: Long,
        val request: Request,
        val submittedAtNanos: Long,
    )

    private class Cancellation : PredictionCancellation {
        private val cancelled = AtomicBoolean(false)
        private val handler = AtomicReference<(() -> Unit)?>(null)
        override fun isCancelled(): Boolean = cancelled.get() || Thread.currentThread().isInterrupted

        override fun installCancellationCallback(callback: () -> Unit) {
            if (cancelled.get()) {
                callback()
            } else {
                handler.set(callback)
                if (cancelled.get() && handler.compareAndSet(callback, null)) callback()
            }
        }

        override fun removeCancellationCallback(callback: () -> Unit) {
            handler.compareAndSet(callback, null)
        }

        fun cancel() {
            if (cancelled.compareAndSet(false, true)) handler.getAndSet(null)?.invoke()
        }
    }

    private val lexicalDelayNanos = TimeUnit.MILLISECONDS.toNanos(lexicalDelayMs.coerceAtLeast(0))
    private val deferredDelayNanos = TimeUnit.MILLISECONDS.toNanos(deferredDelayMs.coerceAtLeast(0))
    private val generation = AtomicLong(0)
    private val latest = AtomicReference<Envelope<Request>?>(null)
    private val activeCancellation = AtomicReference<Cancellation?>(null)
    private val completedLexicalGeneration = AtomicLong(-1)
    private val completedDeferredGeneration = AtomicLong(-1)
    private val closed = AtomicBoolean(false)
    private val worker = Thread(::workerLoop, threadName).apply {
        isDaemon = true
        start()
    }

    /** Constant-time, lock-free publication of the newest immutable request. */
    fun submit(request: Request): Long {
        if (closed.get()) return generation.get()
        val nextGeneration = generation.incrementAndGet()
        latest.set(Envelope(nextGeneration, request, System.nanoTime()))
        activeCancellation.get()?.cancel()
        observer.onMailboxPublished()
        LockSupport.unpark(worker)
        return nextGeneration
    }

    /** Invalidates pending/active work for field, cursor, clear-all, or lifecycle changes. */
    fun invalidate(): Long {
        val nextGeneration = generation.incrementAndGet()
        latest.set(null)
        activeCancellation.get()?.cancel()
        observer.onInvalidated()
        LockSupport.unpark(worker)
        return nextGeneration
    }

    fun isCurrent(candidateGeneration: Long): Boolean =
        !closed.get() && generation.get() == candidateGeneration &&
            latest.get()?.generation == candidateGeneration

    fun diagnostics(): Diagnostics {
        val envelope = latest.get()
        val pendingLexical = envelope != null &&
            completedLexicalGeneration.get() != envelope.generation
        val pendingDeferred = envelope != null &&
            completedDeferredGeneration.get() != envelope.generation
        return Diagnostics(
            generation = generation.get(),
            queuedTasks = (if (pendingLexical) 1 else 0) + (if (pendingDeferred) 1 else 0),
            active = activeCancellation.get() != null,
            hasPendingLexical = pendingLexical,
            hasPendingDeferred = pendingDeferred,
        )
    }

    private fun workerLoop() {
        try {
            while (!closed.get()) {
                val envelope = latest.get()
                if (envelope == null) {
                    LockSupport.park(this)
                    continue
                }
                if (completedLexicalGeneration.get() != envelope.generation) {
                    if (!waitUntilCurrent(envelope, envelope.submittedAtNanos + lexicalDelayNanos)) continue
                    runStage(envelope, PredictionStage.LEXICAL, lexicalWork)
                    completedLexicalGeneration.set(envelope.generation)
                    continue
                }
                if (completedDeferredGeneration.get() != envelope.generation) {
                    if (!waitUntilCurrent(envelope, envelope.submittedAtNanos + deferredDelayNanos)) continue
                    runStage(envelope, PredictionStage.DEFERRED, deferredWork)
                    completedDeferredGeneration.set(envelope.generation)
                    continue
                }
                LockSupport.park(this)
            }
        } finally {
            workerCleanup()
        }
    }

    private fun waitUntilCurrent(envelope: Envelope<Request>, dueAtNanos: Long): Boolean {
        while (true) {
            if (!isEnvelopeCurrent(envelope)) return false
            val remaining = dueAtNanos - System.nanoTime()
            if (remaining <= 0) return true
            LockSupport.parkNanos(this, remaining)
            if (Thread.interrupted() && closed.get()) return false
        }
    }

    private fun runStage(
        envelope: Envelope<Request>,
        stage: PredictionStage,
        work: (Request, PredictionCancellation) -> Result?,
    ) {
        if (!isEnvelopeCurrent(envelope)) return
        val cancellation = Cancellation()
        activeCancellation.getAndSet(cancellation)?.cancel()
        observer.onStageStarted(stage)
        try {
            val result = work(envelope.request, cancellation) ?: return
            if (!cancellation.isCancelled() && isEnvelopeCurrent(envelope)) {
                deliver(envelope.generation, stage, result)
            }
        } finally {
            activeCancellation.compareAndSet(cancellation, null)
            val current = latest.get()
            val remaining = when {
                current == null -> 0
                current === envelope && isEnvelopeCurrent(envelope) ->
                    if (stage == PredictionStage.LEXICAL) 1 else 0
                else -> 2
            }
            observer.onStageFinished(stage, remaining)
        }
    }

    private fun isEnvelopeCurrent(envelope: Envelope<Request>): Boolean =
        !closed.get() && generation.get() == envelope.generation && latest.get() === envelope

    override fun close() {
        if (!closed.compareAndSet(false, true)) return
        generation.incrementAndGet()
        latest.set(null)
        activeCancellation.get()?.cancel()
        observer.onInvalidated()
        worker.interrupt()
        LockSupport.unpark(worker)
    }

    data class Diagnostics(
        val generation: Long,
        val queuedTasks: Int,
        val active: Boolean,
        val hasPendingLexical: Boolean,
        val hasPendingDeferred: Boolean,
    )
}
