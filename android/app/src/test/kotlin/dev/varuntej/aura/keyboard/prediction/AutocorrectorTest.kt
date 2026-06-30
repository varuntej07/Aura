package dev.varuntej.aura.keyboard.prediction

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/** Pure-JVM coverage for the autocorrect decision (keep vs replace, with case preservation). */
class AutocorrectorTest {

    private val source = object : WordSource {
        private val freq = mapOf("the" to 1000, "world" to 500, "hello" to 300, "cat" to 200)
        override fun isKnown(word: String) = freq.containsKey(word)
        override fun frequencyOf(word: String) = freq[word] ?: 0
    }
    private val autocorrector = Autocorrector(SpellChecker(source))

    @Test
    fun knownWord_isKept() {
        assertTrue(autocorrector.onSeparator("hello") is Autocorrector.Decision.Keep)
    }

    @Test
    fun typo_isReplacedWithTheCorrection() {
        val d = autocorrector.onSeparator("teh")
        assertTrue(d is Autocorrector.Decision.Replace)
        d as Autocorrector.Decision.Replace
        assertEquals("teh", d.original)
        assertEquals("the", d.corrected)
    }

    @Test
    fun replacement_preservesCasePattern() {
        assertEquals("The", (autocorrector.onSeparator("Teh") as Autocorrector.Decision.Replace).corrected)
        assertEquals("THE", (autocorrector.onSeparator("TEH") as Autocorrector.Decision.Replace).corrected)
    }

    @Test
    fun typo_withNoConfidentCorrection_isKept() {
        assertTrue(autocorrector.onSeparator("qzxjk") is Autocorrector.Decision.Keep)
    }

    @Test
    fun typo_onlyFixableInTwoEdits_isKeptOnSeparator() {
        // "wld" -> "world" needs two edits. Autocorrect-on-separator is edit-1 only (the two-edit
        // pass is too expensive for the synchronous keystroke path), so it must NOT auto-replace
        // here; the two-edit fix still surfaces in the strip via the off-thread correction pass.
        assertTrue(autocorrector.onSeparator("wld") is Autocorrector.Decision.Keep)
    }

    @Test
    fun applyCasePattern_handlesLowerTitleAndUpper() {
        assertEquals("the", Autocorrector.applyCasePattern("teh", "the"))
        assertEquals("The", Autocorrector.applyCasePattern("Teh", "the"))
        assertEquals("THE", Autocorrector.applyCasePattern("TEH", "the"))
    }
}
