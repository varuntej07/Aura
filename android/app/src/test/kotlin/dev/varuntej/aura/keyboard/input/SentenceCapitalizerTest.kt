package dev.varuntej.aura.keyboard.input

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/** Pure-JVM coverage for auto-capitalization detection. */
class SentenceCapitalizerTest {

    @Test
    fun emptyOrNull_isAStart() {
        assertTrue(SentenceCapitalizer.shouldCapitalize(""))
        assertTrue(SentenceCapitalizer.shouldCapitalize(null))
        assertTrue(SentenceCapitalizer.shouldCapitalize("   ")) // only whitespace
    }

    @Test
    fun midSentence_isNotCapitalized() {
        assertFalse(SentenceCapitalizer.shouldCapitalize("hello "))
        assertFalse(SentenceCapitalizer.shouldCapitalize("hello wor"))
    }

    @Test
    fun afterSentencePunctuation_isCapitalized() {
        assertTrue(SentenceCapitalizer.shouldCapitalize("Hello. "))
        assertTrue(SentenceCapitalizer.shouldCapitalize("What?"))
        assertTrue(SentenceCapitalizer.shouldCapitalize("Wow!  "))
    }

    @Test
    fun afterNewline_isCapitalized() {
        assertTrue(SentenceCapitalizer.shouldCapitalize("first line\n"))
    }

    @Test
    fun trailingSpacesAreIgnored() {
        assertTrue(SentenceCapitalizer.shouldCapitalize("Done.     "))
        assertFalse(SentenceCapitalizer.shouldCapitalize("word     "))
    }
}
