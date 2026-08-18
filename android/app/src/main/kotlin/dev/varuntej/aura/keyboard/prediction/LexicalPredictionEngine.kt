package dev.varuntej.aura.keyboard.prediction

import java.io.Closeable
import kotlin.math.ln

sealed interface PredictionRequest {
    data object Warmup : PredictionRequest
    data class CurrentWord(val rawWord: String, val autocorrectAllowed: Boolean) : PredictionRequest
    data class NextWord(
        val previousWord: String,
        val history: List<String> = listOf(previousWord),
    ) : PredictionRequest
}

data class CachedAutocorrect(
    val rawWord: String,
    val correctedWord: String,
    val confidence: Double,
)

internal object CachedAutocorrectPolicy {
    fun consume(
        decision: CachedAutocorrect?,
        decisionGeneration: Long?,
        activeGeneration: Long,
        rawWord: String,
        manualCorrectionPending: Boolean,
    ): CachedAutocorrect? = decision?.takeIf {
        !manualCorrectionPending && decisionGeneration == activeGeneration && it.rawWord == rawWord
    }
}

data class PredictionPayload(
    val request: PredictionRequest,
    val suggestions: List<Suggestion>,
    val autocorrect: CachedAutocorrect? = null,
    val personalizationGeneration: Long,
)

/** Always-available deterministic tier plus an optional bounded neural reranker. */
internal class LexicalPredictionEngine(
    private val personalDictionary: PersonalDictionary,
    private val neuralScorer: NeuralCandidateScorer? = null,
    private val personalizationEnabled: () -> Boolean = { true },
    private val neuralRerankingEnabled: () -> Boolean = { true },
) : Closeable {
    fun neuralDiagnostics(): NeuralRuntimeDiagnostics? =
        (neuralScorer as? OnDeviceReranker)?.diagnostics()

    fun lexical(
        request: PredictionRequest,
        cancellation: PredictionCancellation,
    ): PredictionPayload? {
        return when (request) {
            PredictionRequest.Warmup -> {
                neuralScorer?.warm(cancellation)
                if (cancellation.isCancelled()) null else PredictionPayload(
                    request,
                    emptyList(),
                    personalizationGeneration = personalDictionary.generation,
                )
            }
            is PredictionRequest.CurrentWord -> currentWord(request, cancellation, includeEditTwo = false)
            is PredictionRequest.NextWord -> nextWord(request, cancellation)
        }
    }

    fun deferred(
        request: PredictionRequest,
        cancellation: PredictionCancellation,
    ): PredictionPayload? {
        val deterministic = when (request) {
            PredictionRequest.Warmup -> return null
            is PredictionRequest.CurrentWord -> currentWord(request, cancellation, includeEditTwo = true)
            is PredictionRequest.NextWord -> nextWord(request, cancellation)
        } ?: return null
        if (cancellation.isCancelled() || deterministic.suggestions.size < 2 ||
            !neuralRerankingEnabled()
        ) {
            return deterministic
        }
        val scores = neuralScorer?.score(request, deterministic.suggestions, cancellation)
        if (cancellation.isCancelled() ||
            personalDictionary.generation != deterministic.personalizationGeneration
        ) {
            return null
        }
        return deterministic.copy(
            suggestions = NeuralRerankPolicy.apply(deterministic.suggestions, scores),
        )
    }

    private fun nextWord(
        request: PredictionRequest.NextWord,
        cancellation: PredictionCancellation,
    ): PredictionPayload? {
        if (cancellation.isCancelled()) return null
        val personalizationGeneration = personalDictionary.generation
        val personal = if (personalizationEnabled()) {
            personalDictionary.nextWords(request.history, SUGGESTION_LIMIT)
        } else {
            emptyList()
        }
        val fallback = NextWordPredictor.predictAfter(request.previousWord, SUGGESTION_LIMIT)
        val words = (personal + fallback).distinct().take(SUGGESTION_LIMIT)
        return if (cancellation.isCancelled() ||
            personalDictionary.generation != personalizationGeneration
        ) {
            null
        } else {
            PredictionPayload(
                request,
                words.map { Suggestion(it, SuggestionSource.NEXT_WORD) },
                personalizationGeneration = personalizationGeneration,
            )
        }
    }

    private fun currentWord(
        request: PredictionRequest.CurrentWord,
        cancellation: PredictionCancellation,
        includeEditTwo: Boolean,
    ): PredictionPayload? {
        if (cancellation.isCancelled()) return null
        val personalizationGeneration = personalDictionary.generation
        val word = request.rawWord
        val base = BaseDictionary.completions(word, SUGGESTION_LIMIT)
        if (cancellation.isCancelled()) return null
        val personal = if (personalizationEnabled()) {
            personalDictionary.completions(word, SUGGESTION_LIMIT) +
                SystemUserDictionary.completions(word, SUGGESTION_LIMIT)
        } else {
            emptyList()
        }
        val completions = SuggestionRanker.rank(
            base = base,
            personal = personal,
            limit = SUGGESTION_LIMIT,
        ).map { suggestion -> suggestion.copy(word = applyCasePattern(word, suggestion.word)) }

        val correction = if (request.autocorrectAllowed && !isKnown(word)) {
            chooseAutocorrect(word, cancellation)
        } else {
            null
        }
        if (cancellation.isCancelled() ||
            personalDictionary.generation != personalizationGeneration
        ) {
            return null
        }
        if (completions.isNotEmpty()) {
            return PredictionPayload(
                request,
                completions,
                correction,
                personalizationGeneration,
            )
        }
        if (!includeEditTwo) {
            return PredictionPayload(
                request,
                emptyList(),
                correction,
                personalizationGeneration,
            )
        }

        val corrections = BaseDictionary.corrections(
            word = word,
            limit = SUGGESTION_LIMIT,
            maxEditDistance = 2,
            cancellation = cancellation,
        ).map { candidate ->
            Suggestion(applyCasePattern(word, candidate.word), SuggestionSource.CORRECTION)
        }
        return if (cancellation.isCancelled() ||
            personalDictionary.generation != personalizationGeneration
        ) {
            null
        } else {
            PredictionPayload(
                request,
                corrections,
                correction,
                personalizationGeneration,
            )
        }
    }

    private fun chooseAutocorrect(
        rawWord: String,
        cancellation: PredictionCancellation,
    ): CachedAutocorrect? {
        if (personalizationEnabled()) {
            personalDictionary.matureCorrectionFor(rawWord)?.let { learned ->
                if (BaseDictionary.contains(learned) || personalDictionary.contains(learned)) {
                    val corrected = applyCasePattern(rawWord, learned)
                    if (corrected != rawWord) {
                        return CachedAutocorrect(rawWord, corrected, MATURE_PERSONAL_CONFIDENCE)
                    }
                }
            }
        }
        val candidates = BaseDictionary.corrections(
            word = rawWord,
            limit = AUTOCORRECT_CANDIDATE_LIMIT,
            maxEditDistance = 1,
            cancellation = cancellation,
        )
        val best = candidates.firstOrNull() ?: return null
        if (best.frequency < AUTOCORRECT_MIN_FREQUENCY) return null
        val bestConfidence = correctionConfidence(best)
        val runnerUpConfidence = candidates.getOrNull(1)?.let(::correctionConfidence)
        if (runnerUpConfidence != null &&
            bestConfidence - runnerUpConfidence < AUTOCORRECT_MIN_CONFIDENCE_MARGIN
        ) {
            return null
        }
        val corrected = applyCasePattern(rawWord, best.word)
        return if (corrected == rawWord) null else CachedAutocorrect(rawWord, corrected, bestConfidence)
    }

    private fun isKnown(word: String): Boolean = BaseDictionary.contains(word) ||
        (personalizationEnabled() &&
            (personalDictionary.contains(word) || SystemUserDictionary.contains(word)))

    private fun correctionConfidence(candidate: CorrectionCandidate): Double =
        ln(candidate.frequency.toDouble() + 1.0) + candidate.proximityScore * PROXIMITY_WEIGHT

    override fun close() {
        (neuralScorer as? Closeable)?.close()
    }

    companion object {
        const val SUGGESTION_LIMIT = 3
        const val AUTOCORRECT_CANDIDATE_LIMIT = 4
        const val AUTOCORRECT_MIN_FREQUENCY = 100
        const val AUTOCORRECT_MIN_CONFIDENCE_MARGIN = 0.30
        const val PROXIMITY_WEIGHT = 0.12
        const val MATURE_PERSONAL_CONFIDENCE = 100.0

        fun applyCasePattern(source: String, targetLower: String): String = when {
            source.isEmpty() -> targetLower
            source.length > 1 && source.all { it.isUpperCase() } -> targetLower.uppercase()
            source.first().isUpperCase() -> targetLower.replaceFirstChar { it.uppercaseChar() }
            else -> targetLower
        }
    }
}
