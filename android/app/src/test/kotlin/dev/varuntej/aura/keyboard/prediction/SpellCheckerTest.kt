package dev.varuntej.aura.keyboard.prediction

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/** Pure-JVM coverage for misspelling detection and single-edit correction. */
class SpellCheckerTest {

    // A tiny in-memory dictionary with frequencies, standing in for base ∪ personal ∪ system.
    private val source = object : WordSource {
        private val freq = mapOf(
            "the" to 1000, "they" to 900, "world" to 500, "word" to 400,
            "hello" to 300, "thiru" to 5, "cat" to 200,
        )
        override fun isKnown(word: String) = freq.containsKey(word)
        override fun frequencyOf(word: String) = freq[word] ?: 0
    }
    private val checker = SpellChecker(source)

    @Test
    fun knownWord_isNotMisspelled() {
        assertFalse(checker.isMisspelled("hello"))
        assertFalse(checker.isMisspelled("Hello")) // case-insensitive membership
        assertFalse(checker.isMisspelled("thiru"))  // a learned/personal word counts as known
    }

    @Test
    fun unknownWord_isMisspelled() {
        assertTrue(checker.isMisspelled("teh"))
        assertTrue(checker.isMisspelled("wrold"))
    }

    @Test
    fun shortWords_areNeverFlagged() {
        assertFalse(checker.isMisspelled("zx")) // below the minimum length
    }

    @Test
    fun corrections_fixASingleEditTypo_rankedByFrequency() {
        assertEquals("the", checker.corrections("teh", 1).first())   // transpose
        assertEquals("world", checker.corrections("wrold", 1).first()) // transpose
        assertEquals("hello", checker.corrections("helo", 1).first()) // insert
    }

    @Test
    fun corrections_respectLimit_andExcludeTheWordItself() {
        val out = checker.corrections("teh", 5)
        assertTrue(out.isNotEmpty())
        assertFalse(out.contains("teh"))
    }

    @Test
    fun corrections_forAGibberishWord_areEmpty() {
        // No known word is one or two edits away from this.
        assertTrue(checker.corrections("qzxjk", 3).isEmpty())
    }

    @Test
    fun corrections_fallBackToEditDistanceTwo_whenNoSingleEditExists() {
        // "wld" -> "world" needs two inserts (o, then r); no known word is a single edit away,
        // so the two-edit fallback must find it.
        assertTrue(checker.corrections("wld", 5).contains("world"))
    }

    @Test
    fun corrections_doNotEscalateToEditTwo_whenASingleEditExists() {
        // "teh" -> "the" is one transpose away, so the expensive two-edit pass must be skipped.
        // A counting source proves it: edits1 of a 3-letter word probes ~190 candidates; edits2
        // would probe tens of thousands.
        val counting = CountingSource(mapOf("the" to 1000))
        val out = SpellChecker(counting).corrections("teh", 3)
        assertEquals("the", out.first())
        assertTrue("edits2 should not run when edits1 hits (probes=${counting.knownProbes})", counting.knownProbes < 1000)
    }

    /** A [WordSource] that counts how many membership probes it answers, to prove the two-edit
     *  pass is gated (it only runs when edit distance 1 finds nothing). */
    private class CountingSource(private val freq: Map<String, Int>) : WordSource {
        var knownProbes = 0
            private set
        override fun isKnown(word: String): Boolean {
            knownProbes++
            return freq.containsKey(word)
        }
        override fun frequencyOf(word: String): Int = freq[word] ?: 0
    }

    @Test
    fun edits1_includesDeleteTransposeReplaceInsert() {
        val set = SpellChecker.edits1("ab").toSet()
        assertTrue(set.contains("b"))    // delete
        assertTrue(set.contains("ba"))   // transpose
        assertTrue(set.contains("cb"))   // replace a->c
        assertTrue(set.contains("xab"))  // insert at front
    }
}
