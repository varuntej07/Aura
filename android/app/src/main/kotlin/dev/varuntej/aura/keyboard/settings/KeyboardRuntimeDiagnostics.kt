package dev.varuntej.aura.keyboard.settings

import dev.varuntej.aura.keyboard.prediction.NeuralRuntimeDiagnostics
import dev.varuntej.aura.keyboard.prediction.NeuralRuntimeState

internal data class KeyboardModelDiagnosticsSnapshot(
    val status: String,
    val provider: String,
    val modelVersion: String,
    val inferenceCount: Long,
    val lastErrorCategory: String,
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
        return KeyboardModelDiagnosticsSnapshot(
            status = status,
            provider = diagnostics?.providerRequested?.name ?: "CPU",
            modelVersion = MODEL_VERSION,
            inferenceCount = diagnostics?.inferenceCount ?: 0L,
            lastErrorCategory = diagnostics?.failureKind ?: "None",
        )
    }

    private const val MODEL_VERSION = "reranker-int8-20260815"
}
