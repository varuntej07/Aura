package dev.varuntej.aura.keyboard.prediction

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class LexicalPredictionEngineTest {
    @Test
    fun clearGenerationDuringPrediction_rejectsOldPersonalizedResult() {
        val personal = FakePersonalDictionary(
            next = listOf("buddy", "there"),
            advanceGenerationDuringRead = true,
        )
        val engine = LexicalPredictionEngine(personal)

        val result = engine.lexical(
            PredictionRequest.NextWord("hello"),
            PredictionCancellation.NEVER,
        )

        assertNull(result)
    }

    @Test
    fun unavailableNeuralTier_preservesExactDeterministicOrder() {
        val personal = FakePersonalDictionary(next = listOf("buddy", "there", "world"))
        val request = PredictionRequest.NextWord("hello")
        val deterministic = LexicalPredictionEngine(personal).deferred(
            request,
            PredictionCancellation.NEVER,
        )
        val unavailable = LexicalPredictionEngine(
            personal,
            NeuralCandidateScorer { _, _, _ -> null },
        ).deferred(request, PredictionCancellation.NEVER)

        assertEquals(deterministic, unavailable)
    }

    @Test
    fun neuralLexicalWinner_preservesExactDeterministicOrder() {
        val personal = FakePersonalDictionary(next = listOf("buddy", "there", "world"))
        val request = PredictionRequest.NextWord("hello")
        val deterministic = LexicalPredictionEngine(personal).deferred(
            request,
            PredictionCancellation.NEVER,
        )
        val enabled = LexicalPredictionEngine(
            personal,
            NeuralCandidateScorer { _, candidates, _ ->
                FloatArray(candidates.size) { index -> if (index == 0) 1f else 0f }
            },
        ).deferred(request, PredictionCancellation.NEVER)

        assertEquals(deterministic, enabled)
    }

    private class FakePersonalDictionary(
        private val next: List<String>,
        private val advanceGenerationDuringRead: Boolean = false,
    ) : PersonalDictionary {
        private var currentGeneration = 0L
        override val generation: Long get() = currentGeneration

        override fun completions(prefix: String, limit: Int): List<WordCandidate> = emptyList()
        override fun contains(word: String): Boolean = false
        override fun nextWords(history: List<String>, limit: Int): List<String> {
            val result = next.take(limit)
            if (advanceGenerationDuringRead) currentGeneration++
            return result
        }
        override fun record(event: PersonalizationEvent) = Unit
        override fun clearAll(onComplete: (Boolean) -> Unit) {
            currentGeneration++
            onComplete(true)
        }
        override fun close() = Unit
    }
}
