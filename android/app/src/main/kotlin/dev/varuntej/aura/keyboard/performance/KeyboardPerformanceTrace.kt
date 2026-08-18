package dev.varuntej.aura.keyboard.performance

import android.os.Build
import android.os.Debug
import android.os.Process
import android.os.SystemClock
import android.os.Trace
import android.net.TrafficStats
import dev.varuntej.aura.keyboard.prediction.NeuralRuntimeDiagnostics
import dev.varuntej.aura.keyboard.prediction.PredictionCoordinatorObserver
import dev.varuntej.aura.keyboard.prediction.PredictionStage
import java.io.File

/** Static-name Perfetto instrumentation with no per-key string/list allocation. */
object KeyboardPerformanceTrace {
    const val KEY_KIND_CHARACTER = 1
    const val KEY_KIND_BACKSPACE = 2
    const val KEY_KIND_FUNCTION = 3

    private var keySequence = 0L
    private var activeSuggestionGeneration = -1L
    private var activeSuggestionStartedNanos = 0L
    private var lexicalSuggestionOpen = false
    private var deferredSuggestionOpen = false

    fun markActionDown(kind: Int) {
        if (!tracingEnabled()) return
        keySequence++
        Trace.setCounter(KEY_SEQUENCE_COUNTER, keySequence)
        Trace.setCounter(KEY_KIND_COUNTER, kind.toLong())
        Trace.beginSection(KEY_DOWN_SECTION)
        Trace.endSection()
    }

    fun beginInputConnectionMutation() {
        Trace.beginSection(INPUT_CONNECTION_SECTION)
    }

    fun endInputConnectionMutation() {
        Trace.endSection()
    }

    fun beginKeyHandler() {
        Trace.beginSection(KEY_HANDLER_SECTION)
    }

    fun endKeyHandler() {
        Trace.endSection()
    }

    /** Captures cumulative process/UID counters only when requested by the controlled benchmark. */
    internal fun captureRuntimeSnapshot(neural: NeuralRuntimeDiagnostics? = null) {
        if (!tracingEnabled()) return
        runtimeStat("art.gc.bytes-allocated")?.let { Trace.setCounter(ALLOCATED_BYTES_COUNTER, it) }
        runtimeStat("art.gc.gc-count")?.let { Trace.setCounter(GC_COUNT_COUNTER, it) }
        val uid = Process.myUid()
        TrafficStats.getUidRxBytes(uid).takeIf { it >= 0 }?.let {
            Trace.setCounter(UID_RX_BYTES_COUNTER, it)
        }
        TrafficStats.getUidTxBytes(uid).takeIf { it >= 0 }?.let {
            Trace.setCounter(UID_TX_BYTES_COUNTER, it)
        }
        processIo().forEach { (name, value) ->
            when (name) {
                "read_bytes" -> Trace.setCounter(PROCESS_READ_BYTES_COUNTER, value)
                "write_bytes" -> Trace.setCounter(PROCESS_WRITE_BYTES_COUNTER, value)
            }
        }
        Trace.setCounter(PROCESS_PSS_KB_COUNTER, Debug.getPss().toLong())
        neural?.let { diagnostics ->
            Trace.setCounter(ORT_STATE_COUNTER, diagnostics.state.ordinal.toLong())
            Trace.setCounter(ORT_PROVIDER_COUNTER, diagnostics.providerRequested.ordinal.toLong())
            Trace.setCounter(ORT_MODEL_BYTES_COUNTER, diagnostics.modelBytes)
            Trace.setCounter(ORT_PARAMETER_COUNT_COUNTER, diagnostics.parameterCount.toLong())
            Trace.setCounter(ORT_INFERENCE_COUNT_COUNTER, diagnostics.inferenceCount)
            diagnostics.initializationMillis?.let {
                Trace.setCounter(ORT_INITIALIZATION_US_COUNTER, (it * 1_000).toLong())
            }
            diagnostics.warmupMillis?.let {
                Trace.setCounter(ORT_WARMUP_US_COUNTER, (it * 1_000).toLong())
            }
            diagnostics.inferenceP50Millis?.let {
                Trace.setCounter(ORT_INFERENCE_P50_US_COUNTER, (it * 1_000).toLong())
            }
            diagnostics.inferenceP95Millis?.let {
                Trace.setCounter(ORT_INFERENCE_P95_US_COUNTER, (it * 1_000).toLong())
            }
            diagnostics.inferenceP99Millis?.let {
                Trace.setCounter(ORT_INFERENCE_P99_US_COUNTER, (it * 1_000).toLong())
            }
            snapshotLabel(ORT_STATE_LABEL_PREFIX + diagnostics.state.name)
            snapshotLabel(ORT_PROVIDER_LABEL_PREFIX + diagnostics.providerRequested.name)
            diagnostics.runtimeVersion?.let { snapshotLabel(ORT_RUNTIME_LABEL_PREFIX + it) }
            diagnostics.failureKind?.let { snapshotLabel(ORT_FAILURE_LABEL_PREFIX + it) }
        }
    }

    /** Measures request publication through main-thread suggestion-strip application. */
    fun beginSuggestionRequest(generation: Long) {
        if (!tracingEnabled()) return
        activeSuggestionGeneration = generation
        activeSuggestionStartedNanos = SystemClock.elapsedRealtimeNanos()
        lexicalSuggestionOpen = true
        deferredSuggestionOpen = true
    }

    fun markSuggestionApplied(generation: Long, stage: PredictionStage) {
        if (!tracingEnabled() || generation != activeSuggestionGeneration) return
        val elapsedMicros = (SystemClock.elapsedRealtimeNanos() - activeSuggestionStartedNanos) / 1_000L
        if (stage == PredictionStage.LEXICAL && lexicalSuggestionOpen) {
            Trace.setCounter(SUGGESTION_LEXICAL_US_COUNTER, elapsedMicros)
            lexicalSuggestionOpen = false
        } else if (stage == PredictionStage.DEFERRED && deferredSuggestionOpen) {
            Trace.setCounter(SUGGESTION_DEFERRED_US_COUNTER, elapsedMicros)
            deferredSuggestionOpen = false
        }
        if (!lexicalSuggestionOpen && !deferredSuggestionOpen) activeSuggestionGeneration = -1L
    }

    fun invalidateSuggestionRequest() {
        activeSuggestionGeneration = -1L
        activeSuggestionStartedNanos = 0L
        lexicalSuggestionOpen = false
        deferredSuggestionOpen = false
    }

    val predictionObserver = object : PredictionCoordinatorObserver {
        private var stageTracing = false

        override fun onMailboxPublished() = queued(2)

        override fun onStageStarted(stage: PredictionStage) {
            stageTracing = tracingEnabled()
            if (!stageTracing) return
            Trace.setCounter(PREDICTION_ACTIVE_COUNTER, 1L)
            Trace.beginSection(
                if (stage == PredictionStage.LEXICAL) LEXICAL_SECTION else DEFERRED_SECTION,
            )
        }

        override fun onStageFinished(stage: PredictionStage, currentRequestRemainingStages: Int) {
            if (stageTracing) Trace.endSection()
            if (stageTracing) Trace.setCounter(PREDICTION_ACTIVE_COUNTER, 0L)
            stageTracing = false
            queued(currentRequestRemainingStages)
        }

        override fun onInvalidated() = queued(0)

        private fun queued(value: Int) {
            if (tracingEnabled()) {
                Trace.setCounter(PREDICTION_QUEUE_COUNTER, value.toLong())
            }
        }
    }

    private fun tracingEnabled(): Boolean =
        Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q && Trace.isEnabled()

    private fun runtimeStat(name: String): Long? =
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
            Debug.getRuntimeStat(name)?.toLongOrNull()
        } else {
            null
        }

    private fun processIo(): Map<String, Long> = try {
        File("/proc/self/io").useLines { lines ->
            lines.mapNotNull { line ->
                val separator = line.indexOf(':')
                if (separator <= 0) null else {
                    line.substring(0, separator) to
                        (line.substring(separator + 1).trim().toLongOrNull() ?: return@mapNotNull null)
                }
            }.toMap()
        }
    } catch (_: Throwable) {
        emptyMap()
    }

    private fun snapshotLabel(name: String) {
        Trace.beginSection(name)
        Trace.endSection()
    }

    private const val KEY_SEQUENCE_COUNTER = "AuraIme key sequence"
    private const val KEY_KIND_COUNTER = "AuraIme key kind"
    private const val PREDICTION_QUEUE_COUNTER = "AuraIme prediction pending"
    private const val PREDICTION_ACTIVE_COUNTER = "AuraIme prediction active"
    private const val KEY_DOWN_SECTION = "AuraIme:key ACTION_DOWN"
    private const val KEY_HANDLER_SECTION = "AuraIme:key handler"
    private const val INPUT_CONNECTION_SECTION = "AuraIme:InputConnection mutation"
    private const val LEXICAL_SECTION = "AuraIme:lexical prediction"
    private const val DEFERRED_SECTION = "AuraIme:deferred prediction"
    private const val ALLOCATED_BYTES_COUNTER = "AuraIme runtime allocated bytes"
    private const val GC_COUNT_COUNTER = "AuraIme runtime GC count"
    private const val UID_RX_BYTES_COUNTER = "AuraIme UID RX bytes"
    private const val UID_TX_BYTES_COUNTER = "AuraIme UID TX bytes"
    private const val PROCESS_READ_BYTES_COUNTER = "AuraIme process read bytes"
    private const val PROCESS_WRITE_BYTES_COUNTER = "AuraIme process write bytes"
    private const val PROCESS_PSS_KB_COUNTER = "AuraIme process PSS KB"
    private const val ORT_STATE_COUNTER = "AuraIme ORT state"
    private const val ORT_PROVIDER_COUNTER = "AuraIme ORT provider"
    private const val ORT_MODEL_BYTES_COUNTER = "AuraIme ORT model bytes"
    private const val ORT_PARAMETER_COUNT_COUNTER = "AuraIme ORT parameter count"
    private const val ORT_INFERENCE_COUNT_COUNTER = "AuraIme ORT inference count"
    private const val ORT_INITIALIZATION_US_COUNTER = "AuraIme ORT initialization us"
    private const val ORT_WARMUP_US_COUNTER = "AuraIme ORT warmup us"
    private const val ORT_INFERENCE_P50_US_COUNTER = "AuraIme ORT inference p50 us"
    private const val ORT_INFERENCE_P95_US_COUNTER = "AuraIme ORT inference p95 us"
    private const val ORT_INFERENCE_P99_US_COUNTER = "AuraIme ORT inference p99 us"
    private const val ORT_STATE_LABEL_PREFIX = "AuraIme:ORT state:"
    private const val ORT_PROVIDER_LABEL_PREFIX = "AuraIme:ORT provider:"
    private const val ORT_RUNTIME_LABEL_PREFIX = "AuraIme:ORT runtime:"
    private const val ORT_FAILURE_LABEL_PREFIX = "AuraIme:ORT failure:"
    private const val SUGGESTION_LEXICAL_US_COUNTER = "AuraIme suggestion lexical latency us"
    private const val SUGGESTION_DEFERRED_US_COUNTER = "AuraIme suggestion deferred latency us"
}
