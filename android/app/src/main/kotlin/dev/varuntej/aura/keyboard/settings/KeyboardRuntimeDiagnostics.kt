package dev.varuntej.aura.keyboard.settings

import dev.varuntej.aura.keyboard.prediction.NeuralRerankMetrics
import dev.varuntej.aura.keyboard.prediction.NeuralRuntimeDiagnostics
import dev.varuntej.aura.keyboard.prediction.NeuralRuntimeState

internal data class KeyboardModelDiagnosticsSnapshot(
    val status: String,
    val provider: String,
    val modelVersion: String,
    val inferenceCount: Long,
    val lastErrorCategory: String,
    // Whether the ONNX tier is earning its ~28 MB. See NeuralRerankMetrics.
    val rerankAttempts: Long,
    val rerankTopOneChanges: String,
    val rerankLexicalFallbacks: Long,
)

/**
 * Process-memory diagnostics only. It never accepts typed text and never writes diagnostics to
 * disk or logs. The settings activity can inspect the live IME when both share the app process.
 */
internal object KeyboardRuntimeDiagnostics {
    private data class Registration(
        val owner: Any,
        val provider: () -> NeuralRuntimeDiagnostics?,
    )

    @Volatile
    private var registration: Registration? = null

    @Synchronized
    internal fun attach(owner: Any, provider: () -> NeuralRuntimeDiagnostics?) {
        registration = Registration(owner, provider)
    }

    @Synchronized
    internal fun detach(owner: Any) {
        if (registration?.owner === owner) registration = null
    }

    internal fun snapshot(): KeyboardModelDiagnosticsSnapshot {
        val diagnostics = try {
            registration?.provider?.invoke()
        } catch (_: Throwable) {
            null
        }
        val status = when (diagnostics?.state) {
            NeuralRuntimeState.READY -> "Ready"
            NeuralRuntimeState.FAILED -> "Error — lexical fallback active"
            NeuralRuntimeState.CLOSED -> "Fallback — runtime closed"
            NeuralRuntimeState.UNINITIALIZED, null -> "Fallback — not initialized this session"
        }
        val rerank = NeuralRerankMetrics.snapshot()
        val changeRate = rerank.topOneChangeRate
        return KeyboardModelDiagnosticsSnapshot(
            status = status,
            provider = diagnostics?.providerRequested?.name ?: "CPU",
            modelVersion = MODEL_VERSION,
            inferenceCount = diagnostics?.inferenceCount ?: 0L,
            lastErrorCategory = diagnostics?.failureKind ?: "None",
            rerankAttempts = rerank.attempts,
            rerankTopOneChanges = if (changeRate == null) {
                rerank.topOneChanges.toString()
            } else {
                "%d (%.1f%%)".format(rerank.topOneChanges, changeRate)
            },
            rerankLexicalFallbacks = rerank.lexicalFallbacks,
        )
    }

    private const val MODEL_VERSION = "reranker-int8-20260815"
}
