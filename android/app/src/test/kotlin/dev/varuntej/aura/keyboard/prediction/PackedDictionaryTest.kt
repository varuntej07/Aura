package dev.varuntej.aura.keyboard.prediction

import java.io.File
import java.nio.ByteBuffer
import kotlin.math.ln
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class PackedDictionaryTest {
    private val dictionary: PackedDictionary by lazy {
        PackedDictionary.from(ByteBuffer.wrap(dictionaryAsset().readBytes()))
    }

    @Test
    fun packagedDictionary_hasExpectedCorpusAndCachedBound() {
        assertEquals(30_000, dictionary.wordCount)
        assertEquals(8, dictionary.maxCachedCompletions)
        assertTrue(dictionary.nodeCount > 0)
        assertTrue(dictionary.edgeCount > 0)
    }

    @Test
    fun completions_areRanked_caseInsensitive_andBoundedByPrefixPlusK() {
        val lower = dictionary.completionsWithStats("the", 3)
        val upper = dictionary.completionsWithStats("THE", 3)
        assertEquals(lower.candidates, upper.candidates)
        assertEquals(3, lower.candidates.size)
        assertEquals("the", lower.candidates.first().word)
        assertTrue(lower.labelCharacters <= 3)
        // Each node has at most 26 sorted edges, so binary search compares at most five.
        assertTrue(lower.edgeComparisons <= 3 * 5)
    }

    @Test
    fun membershipAndFrequency_areExact() {
        assertTrue(dictionary.contains("Aura"))
        assertFalse(dictionary.contains("aur"))
        assertTrue(dictionary.frequencyOf("the") > dictionary.frequencyOf("abalone"))
        assertEquals(0, dictionary.frequencyOf("notawordzzzz"))
    }

    @Test
    fun correctionTraversal_findsTransposeWithoutMaterializingEdits() {
        val result = dictionary.corrections("teh", 3, maxEditDistance = 1)
        assertTrue(result.any { it.word == "the" && it.editDistance == 1 })
    }

    @Test
    fun deterministicTehCorrectionClearsTheProductionConfidenceGate() {
        val result = dictionary.corrections(
            "teh",
            LexicalPredictionEngine.AUTOCORRECT_CANDIDATE_LIMIT,
            maxEditDistance = 1,
        )
        val winner = result.first()
        val winnerConfidence = ln(winner.frequency.toDouble() + 1.0) +
            winner.proximityScore * LexicalPredictionEngine.PROXIMITY_WEIGHT
        val runnerUpConfidence = result.getOrNull(1)?.let { candidate ->
            ln(candidate.frequency.toDouble() + 1.0) +
                candidate.proximityScore * LexicalPredictionEngine.PROXIMITY_WEIGHT
        }

        assertEquals("the", winner.word)
        assertTrue(winner.frequency >= LexicalPredictionEngine.AUTOCORRECT_MIN_FREQUENCY)
        assertTrue(
            runnerUpConfidence == null ||
                winnerConfidence - runnerUpConfidence >=
                LexicalPredictionEngine.AUTOCORRECT_MIN_CONFIDENCE_MARGIN,
        )
    }

    @Test
    fun correctionTraversal_honorsCooperativeCancellation() {
        val result = dictionary.corrections(
            "definately",
            3,
            maxEditDistance = 2,
            cancellation = PredictionCancellation { true },
        )
        assertTrue(result.isEmpty())
    }

    @Test(expected = IllegalArgumentException::class)
    fun corruptHeader_failsClosed() {
        PackedDictionary.from(ByteBuffer.wrap(ByteArray(128)))
    }

    private fun dictionaryAsset(): File {
        val candidates = listOf(
            File("src/main/assets/dictionaries/en_us.pdict"),
            File("app/src/main/assets/dictionaries/en_us.pdict"),
            File("android/app/src/main/assets/dictionaries/en_us.pdict"),
        )
        return candidates.firstOrNull(File::isFile)
            ?: error("packed dictionary asset not found from ${File(".").absolutePath}")
    }
}
