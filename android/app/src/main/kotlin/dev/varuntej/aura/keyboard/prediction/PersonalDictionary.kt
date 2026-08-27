package dev.varuntej.aura.keyboard.prediction

import android.content.Context
import java.util.concurrent.ConcurrentLinkedQueue
import java.util.concurrent.CopyOnWriteArraySet
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.atomic.AtomicLong
import java.util.concurrent.locks.LockSupport

interface PersonalDictionary {
    val generation: Long get() = 0L
    fun completions(prefix: String, limit: Int): List<WordCandidate>
    fun contains(word: String): Boolean
    fun nextWords(history: List<String>, limit: Int): List<String> = emptyList()
    fun matureCorrectionFor(rawWord: String): String? = null
    fun record(event: PersonalizationEvent)
    fun clearAll(onComplete: (Boolean) -> Unit = {})

    fun learn(word: String) = record(
        PersonalizationEvent.ManualWordCommitted(word, null, null, System.currentTimeMillis()),
    )

    fun add(word: String) = record(PersonalizationEvent.ExplicitAdd(word, System.currentTimeMillis()))
    fun remove(word: String) = record(PersonalizationEvent.ExplicitRemove(word, System.currentTimeMillis()))
    fun close()
}

/**
 * One process-local owner shared by the IME and its settings activity. Sharing prevents a settings
 * clear from racing an older dictionary instance that could otherwise persist learned state again.
 */
object KeyboardPersonalizationRepository {
    private val generationListeners = CopyOnWriteArraySet<(Long) -> Unit>()

    @Volatile
    private var instance: LocalPersonalizationDictionary? = null

    fun dictionary(context: Context): LocalPersonalizationDictionary =
        instance ?: synchronized(this) {
            instance ?: LocalPersonalizationDictionary(
                context.applicationContext,
                ::notifyGenerationChanged,
            ).also { instance = it }
        }

    fun addGenerationListener(listener: (Long) -> Unit) {
        generationListeners.add(listener)
    }

    fun removeGenerationListener(listener: (Long) -> Unit) {
        generationListeners.remove(listener)
    }

    private fun notifyGenerationChanged(generation: Long) {
        generationListeners.forEach { listener ->
            try {
                listener(generation)
            } catch (_: Throwable) {
                // A dead UI observer cannot break the shared personalization worker.
            }
        }
    }
}

/**
 * Bounded, encrypted, provenance-aware local personalization.
 *
 * The IME thread only reads the current immutable snapshot and offers commands into a lock-free,
 * logically bounded mailbox. One parked worker owns mutation, maturation timing, snapshot builds,
 * migration, and coalesced encrypted persistence. Queue overflow or any storage/Keystore failure
 * drops learning, never input. Clear-all advances an epoch before publication so stale commands
 * and writes cannot resurrect deleted data.
 */
class LocalPersonalizationDictionary(
    context: Context,
    private val onGenerationChanged: (Long) -> Unit = {},
) : PersonalDictionary {
    private sealed interface Command {
        data class Record(val epoch: Long, val event: PersonalizationEvent) : Command
        data class Clear(
            val epoch: Long,
            val generation: Long,
            val onComplete: (Boolean) -> Unit,
        ) : Command
        data object Close : Command
    }

    @Volatile
    private var snapshot = PersonalizationSnapshot.EMPTY

    @Volatile
    internal var storeStatus: PersonalizationStoreStatus = PersonalizationStoreStatus.EMPTY
        private set

    @Volatile
    internal var encryptedSizeBytes: Long = 0
        private set

    private val store = EncryptedPersonalizationStore(context.applicationContext)
    private val commands = ConcurrentLinkedQueue<Command>()
    private val queuedRecords = AtomicInteger(0)
    private val clearEpoch = AtomicLong(0)
    private val accepting = AtomicBoolean(true)
    private val publicationLock = Any()
    private val generationClock = AtomicLong(0)

    // Worker-owned below this line.
    private var state = PersonalizationState()
    private var persistDueAtNanos = Long.MAX_VALUE
    private var appliedEpoch = 0L
    private val worker = Thread(::workerLoop, "AuraImePersonalization").apply {
        isDaemon = true
        start()
    }

    override val generation: Long
        get() = snapshot.generation

    override fun completions(prefix: String, limit: Int): List<WordCandidate> =
        snapshot.prefixIndex.completions(prefix, limit)

    override fun contains(word: String): Boolean =
        PersonalizationPolicy.normalizeLearnableToken(word)?.let(snapshot.lexemeKeys::contains) == true

    override fun nextWords(history: List<String>, limit: Int): List<String> =
        snapshot.nextWords(history, limit)

    override fun matureCorrectionFor(rawWord: String): String? {
        val key = PersonalizationPolicy.normalizeLearnableToken(rawWord) ?: return null
        return snapshot.matureCorrections[key]?.finalWord
    }

    override fun record(event: PersonalizationEvent) {
        if (!accepting.get()) return
        while (true) {
            val size = queuedRecords.get()
            if (size >= PersonalizationPolicy.MAX_EVENT_QUEUE) return
            if (queuedRecords.compareAndSet(size, size + 1)) break
        }
        commands.offer(Command.Record(clearEpoch.get(), event))
        LockSupport.unpark(worker)
    }

    override fun clearAll(onComplete: (Boolean) -> Unit) {
        if (!accepting.get()) {
            onComplete(false)
            return
        }
        val (epoch, generation) = synchronized(publicationLock) {
            val nextEpoch = clearEpoch.incrementAndGet()
            val nextGeneration = generationClock.incrementAndGet()
            snapshot = PersonalizationSnapshot.EMPTY.copy(generation = nextGeneration)
            nextEpoch to nextGeneration
        }
        notifyGenerationChanged(generation)
        commands.offer(Command.Clear(epoch, generation, onComplete))
        LockSupport.unpark(worker)
    }

    override fun close() {
        if (!accepting.compareAndSet(true, false)) return
        commands.offer(Command.Close)
        LockSupport.unpark(worker)
    }

    private fun workerLoop() {
        val startupEpoch = appliedEpoch
        val loaded = store.loadOrMigrate()
        state = loaded.state
        storeStatus = loaded.status
        encryptedSizeBytes = store.encryptedSizeBytes()
        generationClock.accumulateAndGet(state.generation) { current, stored ->
            maxOf(current, stored)
        }
        publishSnapshot(System.currentTimeMillis(), startupEpoch)

        while (true) {
            var closeRequested = false
            while (true) {
                when (val command = commands.poll() ?: break) {
                    is Command.Record -> {
                        queuedRecords.decrementAndGet()
                        if (command.epoch == clearEpoch.get() && command.epoch == appliedEpoch) {
                            process(command)
                        }
                    }
                    is Command.Clear -> {
                        if (command.epoch == clearEpoch.get()) process(command)
                        else notifyClearComplete(command, success = false)
                    }
                    Command.Close -> closeRequested = true
                }
            }

            val nowMillis = System.currentTimeMillis()
            val maturationEpoch = appliedEpoch
            if (maturationEpoch == clearEpoch.get()) {
                val dueIds = state.pending.values.asSequence()
                    .filter { it.dueAtMillis <= nowMillis }
                    .map(PendingPositive::id)
                    .toList()
                var matured = false
                for (id in dueIds) {
                    matured = PersonalizationReducer.mature(state, id, nowMillis) || matured
                }
                if (matured && publishSnapshot(nowMillis, maturationEpoch)) schedulePersist()
            }

            if (System.nanoTime() >= persistDueAtNanos) persistNow()
            if (closeRequested) {
                persistNow()
                return
            }

            val waitNanos = nextWaitNanos(nowMillis)
            if (commands.isEmpty()) {
                if (waitNanos == Long.MAX_VALUE) LockSupport.park(this)
                else LockSupport.parkNanos(this, waitNanos.coerceAtLeast(1))
            }
        }
    }

    private fun process(command: Command.Record) {
        val mutation = PersonalizationReducer.record(state, command.event)
        if (command.event !is PersonalizationEvent.ManualWordCommitted &&
            command.event !is PersonalizationEvent.SuggestionAccepted &&
            command.event !is PersonalizationEvent.AutomaticCorrection
        ) {
            publishSnapshot(command.event.atMillis, command.epoch)
        }
        if (mutation.changed) schedulePersist()
    }

    private fun process(command: Command.Clear) {
        persistDueAtNanos = Long.MAX_VALUE
        val nextGeneration = synchronized(publicationLock) {
            if (command.epoch != clearEpoch.get()) return notifyClearComplete(command, success = false)
            val generation = generationClock.updateAndGet { current ->
                if (current <= command.generation) command.generation else current + 1
            }
            state = PersonalizationState(generation = generation)
            appliedEpoch = command.epoch
            snapshot = PersonalizationSnapshot.EMPTY.copy(generation = generation)
            generation
        }
        if (nextGeneration != command.generation) notifyGenerationChanged(nextGeneration)
        val success = try {
            store.clearAll()
            storeStatus = PersonalizationStoreStatus.EMPTY
            encryptedSizeBytes = 0
            true
        } catch (_: Throwable) {
            storeStatus = PersonalizationStoreStatus.MEMORY_ONLY
            false
        }
        notifyClearComplete(command, success)
    }

    private fun notifyClearComplete(command: Command.Clear, success: Boolean) {
        try {
            command.onComplete(success)
        } catch (_: Throwable) {
            // A UI/test observer must never terminate the personalization storage worker.
        }
    }

    private fun schedulePersist() {
        persistDueAtNanos = System.nanoTime() +
            TimeUnit.MILLISECONDS.toNanos(PersonalizationPolicy.PERSIST_IDLE_MS)
    }

    private fun persistNow() {
        if (appliedEpoch != clearEpoch.get()) return
        if (persistDueAtNanos == Long.MAX_VALUE && storeStatus != PersonalizationStoreStatus.MEMORY_ONLY) {
            return
        }
        persistDueAtNanos = Long.MAX_VALUE
        try {
            store.save(state)
            encryptedSizeBytes = store.encryptedSizeBytes()
            if (storeStatus == PersonalizationStoreStatus.MEMORY_ONLY ||
                storeStatus == PersonalizationStoreStatus.EMPTY
            ) {
                storeStatus = PersonalizationStoreStatus.LOADED
            }
        } catch (_: Throwable) {
            storeStatus = PersonalizationStoreStatus.MEMORY_ONLY
        }
    }

    private fun nextWaitNanos(nowMillis: Long): Long {
        val nextPendingMillis = state.pending.values.minOfOrNull(PendingPositive::dueAtMillis)
        val pendingWait = nextPendingMillis?.let {
            TimeUnit.MILLISECONDS.toNanos((it - nowMillis).coerceAtLeast(0))
        } ?: Long.MAX_VALUE
        val persistWait = if (persistDueAtNanos == Long.MAX_VALUE) {
            Long.MAX_VALUE
        } else {
            (persistDueAtNanos - System.nanoTime()).coerceAtLeast(0)
        }
        return minOf(pendingWait, persistWait)
    }

    private fun publishSnapshot(nowMillis: Long, expectedEpoch: Long): Boolean {
        val nextGeneration = generationClock.incrementAndGet()
        state.generation = nextGeneration
        val nextSnapshot = state.snapshot(nowMillis)
        val published = synchronized(publicationLock) {
            if (expectedEpoch != clearEpoch.get() || expectedEpoch != appliedEpoch) {
                false
            } else {
                snapshot = nextSnapshot
                true
            }
        }
        if (published) notifyGenerationChanged(nextGeneration)
        return published
    }

    private fun notifyGenerationChanged(generation: Long) {
        try {
            onGenerationChanged(generation)
        } catch (_: Throwable) {
            // An observer must never break typing or terminate the personalization worker.
        }
    }
}
