package dev.varuntej.aura.keyboard.prediction

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/** Pure-JVM coverage for the next-word ranking core (asset loading is exercised on device). */
class NextWordPredictorTest {

    private val unigrams = listOf("the", "to", "and", "a", "of")

    @Test
    fun bigramHit_returnsItsContinuations() {
        val bigrams = mapOf("i" to listOf("am", "will", "have"))
        assertEquals(listOf("am", "will", "have"), NextWordPredictor.rank("I", bigrams, unigrams, 3))
    }

    @Test
    fun bigramHit_respectsLimit() {
        val bigrams = mapOf("i" to listOf("am", "will", "have"))
        assertEquals(listOf("am", "will"), NextWordPredictor.rank("i", bigrams, unigrams, 2))
    }

    @Test
    fun noBigram_fallsBackToUnigramPrior() {
        assertEquals(listOf("the", "to", "and"), NextWordPredictor.rank("zzz", emptyMap(), unigrams, 3))
    }

    @Test
    fun fallback_excludesThePreviousWord() {
        val out = NextWordPredictor.rank("the", emptyMap(), unigrams, 5)
        assertFalse(out.contains("the"))
        assertEquals(listOf("to", "and", "a", "of"), out)
    }

    @Test
    fun zeroLimit_isEmpty() {
        assertTrue(NextWordPredictor.rank("i", emptyMap(), unigrams, 0).isEmpty())
    }
}
