package dev.varuntej.aura.keyboard.prediction

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/** Pure-JVM coverage for the frequency-ranked prefix index behind word completion. */
class PrefixIndexTest {

    // A tiny embedded list (the real one is the 30k-word asset, loaded at runtime).
    private val index = PrefixIndex.from(
        listOf(
            WordCandidate("the", 1000),
            WordCandidate("there", 800),
            WordCandidate("their", 700),
            WordCandidate("then", 500),
            WordCandidate("they", 900),
            WordCandidate("though", 200),
            WordCandidate("cat", 300),
            WordCandidate("car", 400),
            WordCandidate("care", 350),
            WordCandidate("a", 5000),
            WordCandidate("i", 4000),
        ),
    )

    @Test
    fun completions_areOrderedByFrequency() {
        val out = index.completions("the", limit = 3).map { it.word }
        // "the"(1000) > "they"(900) > "there"(800); "their"/"then" fall outside top 3.
        assertEquals(listOf("the", "they", "there"), out)
    }

    @Test
    fun completions_respectTheLimit() {
        assertEquals(3, index.completions("the", limit = 3).size)
        assertEquals(1, index.completions("the", limit = 1).size)
        assertEquals("the", index.completions("the", limit = 1).first().word)
    }

    @Test
    fun completions_matchTheWholePrefixRange() {
        val out = index.completions("ca", limit = 5).map { it.word }.toSet()
        assertEquals(setOf("car", "care", "cat"), out)
    }

    @Test
    fun completions_areCaseInsensitiveOnTheQuery() {
        assertEquals(
            index.completions("the", 3).map { it.word },
            index.completions("THE", 3).map { it.word },
        )
    }

    @Test
    fun completions_emptyPrefixReturnsNothing() {
        assertTrue(index.completions("", 3).isEmpty())
    }

    @Test
    fun completions_unknownPrefixReturnsNothing() {
        assertTrue(index.completions("zzz", 3).isEmpty())
    }

    @Test
    fun completions_includeTheExactWordItself() {
        // Typing a full word still offers it (so an exact match can rank/anchor the strip).
        assertTrue(index.completions("car", 5).any { it.word == "car" })
    }

    @Test
    fun contains_isExactAndCaseInsensitive() {
        assertTrue(index.contains("there"))
        assertTrue(index.contains("There"))
        assertFalse(index.contains("ther"))
        assertFalse(index.contains("zebra"))
    }

    @Test
    fun frequencyOf_returnsCountOrZero() {
        assertEquals(900, index.frequencyOf("they"))
        assertEquals(0, index.frequencyOf("nope"))
    }

    @Test
    fun storedMixedCaseWords_areMatchedCaseInsensitively_displayPreserved() {
        // Provider / vocab words arrive in their natural casing ("KCR", "Aura"). They must be
        // matchable by a lowercase query (the squiggle/known-word path lowercases) yet surfaced in
        // their real casing, so a friend's or interest's name is never flagged or shown lowercase.
        val idx = PrefixIndex.from(
            listOf(WordCandidate("KCR", 50), WordCandidate("Aura", 30)),
        )
        assertTrue(idx.contains("kcr"))
        assertTrue(idx.contains("KCR"))
        assertTrue(idx.contains("aura"))
        assertEquals("KCR", idx.completions("kc", 3).first().word)
        assertEquals("Aura", idx.completions("au", 3).first().word)
    }

    @Test
    fun emptyIndex_isSafe() {
        val empty = PrefixIndex.from(emptyList())
        assertEquals(0, empty.size)
        assertTrue(empty.completions("a", 3).isEmpty())
        assertFalse(empty.contains("a"))
        assertEquals(0, empty.frequencyOf("a"))
    }
}
