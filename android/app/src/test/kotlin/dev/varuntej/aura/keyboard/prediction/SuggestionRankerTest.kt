package dev.varuntej.aura.keyboard.prediction

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/** Pure-JVM coverage for how base / personal / vocab candidates are merged into the strip. */
class SuggestionRankerTest {

    @Test
    fun baseOnly_isOrderedByFrequency_andLimited() {
        val out = SuggestionRanker.rank(
            base = listOf(
                WordCandidate("there", 800),
                WordCandidate("the", 1000),
                WordCandidate("they", 900),
                WordCandidate("their", 700),
            ),
            limit = 3,
        )
        assertEquals(listOf("the", "they", "there"), out.map { it.word })
        assertTrue(out.all { it.source == SuggestionSource.BASE })
    }

    @Test
    fun personalWord_beatsAMoreFrequentBaseWord() {
        // The whole point of learning: a word the user actually types should win its prefix
        // even when a common dictionary word shares it.
        val out = SuggestionRanker.rank(
            base = listOf(WordCandidate("ther", 50_000), WordCandidate("the", 1_000_000)),
            personal = listOf(WordCandidate("thiru", 3)),
            limit = 1,
        )
        assertEquals("thiru", out.first().word)
        assertEquals(SuggestionSource.PERSONAL, out.first().source)
    }

    @Test
    fun vocabWord_outranksPersonalAndBase() {
        // A user's own person/topic term (e.g. a friend's name) should top its prefix.
        val out = SuggestionRanker.rank(
            base = listOf(WordCandidate("kevin", 9000)),
            personal = listOf(WordCandidate("kev", 40)),
            vocab = listOf(WordCandidate("kcr", 1)),
            limit = 3,
        )
        assertEquals("kcr", out.first().word)
        assertEquals(SuggestionSource.VOCAB, out.first().source)
    }

    @Test
    fun sameWordInTwoSources_keepsTheStrongerSource_andDeduplicates() {
        val out = SuggestionRanker.rank(
            base = listOf(WordCandidate("buddy", 5000)),
            personal = listOf(WordCandidate("buddy", 2)),
            limit = 5,
        )
        assertEquals(1, out.size)
        assertEquals("buddy", out.first().word)
        assertEquals(SuggestionSource.PERSONAL, out.first().source)
    }

    @Test
    fun emptyInputs_returnNothing() {
        assertTrue(SuggestionRanker.rank(base = emptyList()).isEmpty())
    }

    @Test
    fun nonPositiveLimit_returnsNothing() {
        assertTrue(
            SuggestionRanker.rank(base = listOf(WordCandidate("hi", 10)), limit = 0).isEmpty(),
        )
    }
}
