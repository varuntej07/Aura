package dev.varuntej.aura.keyboard.prediction

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class PersonalPrefixIndexTest {
    @Test
    fun lookupUsesCachedPrefixRankingAndPreservesCasing() {
        val index = PersonalPrefixIndex.from(
            listOf(
                lexeme("Thiru", manual = 4, at = 10),
                lexeme("Think", manual = 2, at = 20),
                lexeme("There", manual = 1, at = 30),
            ),
            nowMillis = 30,
        )
        assertEquals(listOf("Thiru", "Think"), index.completions("thi", 2).map { it.word })
        assertTrue(index.contains("THIRU"))
        assertFalse(index.contains("thir"))
    }

    @Test
    fun broadPrefixReturnsOnlyTheEightCachedWinners() {
        val index = PersonalPrefixIndex.from(
            (0 until 32).map { value ->
                lexeme("a" + value.toString().padStart(2, '0'), manual = value + 1, at = value.toLong())
            },
            nowMillis = 32,
        )

        val completions = index.completions("a", 100)

        assertEquals(8, completions.size)
        assertEquals("a31", completions.first().word)
        assertEquals("a24", completions.last().word)
    }

    private fun lexeme(word: String, manual: Int, at: Long) = PersonalLexeme(
        key = word.lowercase(),
        display = word,
        manualCount = manual,
        acceptedCount = 0,
        explicitCount = 0,
        legacyCount = 0,
        lastUsedMillis = at,
    )
}
