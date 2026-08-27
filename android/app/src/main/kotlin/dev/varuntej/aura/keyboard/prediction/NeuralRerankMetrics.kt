package dev.varuntej.aura.keyboard.prediction

import java.util.concurrent.atomic.AtomicLong

/**
 * Did the on-device reranker actually change what the user saw?
 *
 * The ONNX tier costs roughly 28 MB of `libonnxruntime.so` per install to run a 121-parameter
 * model whose only evidence is its own synthetic holdout. Nobody can currently say whether a
 * real person's suggestion strip improves at all, so nobody can say whether the 28 MB is worth
 * it. These counters are what turns that question into a measurement during the beta.
 *
 * Content-free by construction: three counters, no words, no text, no disk, no logs. Process
 * memory only, reset every time the keyboard process starts, exactly like
 * [NeuralRuntimeDiagnostics]. Written on the single prediction worker and read from the settings
 * screen, so plain atomics are sufficient and no lock ever touches the typing path.
 */
internal object NeuralRerankMetrics {
    private val attempts = AtomicLong()
    private val lexicalFallbacks = AtomicLong()
    private val topOneChanges = AtomicLong()

    /**
     * @param scoresAvailable false when the model returned nothing and ranking stayed purely
     *   lexical, which is the case the "is ONNX earning its size" question hinges on.
     * @param topOneChanged whether the suggestion in the first strip slot is a different word
     *   than lexical ranking alone would have put there.
     */
    fun record(scoresAvailable: Boolean, topOneChanged: Boolean) {
        attempts.incrementAndGet()
        if (!scoresAvailable) lexicalFallbacks.incrementAndGet()
        if (topOneChanged) topOneChanges.incrementAndGet()
    }

    fun snapshot(): NeuralRerankCounters = NeuralRerankCounters(
        attempts = attempts.get(),
        lexicalFallbacks = lexicalFallbacks.get(),
        topOneChanges = topOneChanges.get(),
    )
}

internal data class NeuralRerankCounters(
    val attempts: Long,
    val lexicalFallbacks: Long,
    val topOneChanges: Long,
) {
    /** Share of reranks that moved a different word into the first slot, as a percentage. */
    val topOneChangeRate: Double?
        get() = if (attempts == 0L) null else topOneChanges * 100.0 / attempts
}
