package dev.varuntej.aura.keyboard.input

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/** Pure-JVM coverage for the long-press alternates map. */
class KeyPopupOptionsTest {

    @Test
    fun vowel_hasAccents_withDigitFirstForTopRow() {
        val e = KeyPopupOptions.alternatesFor("e")
        assertEquals("3", e.first())       // top-row digit comes first
        assertTrue(e.contains("é"))
    }

    @Test
    fun topRowConsonant_hasOnlyItsDigit() {
        assertEquals(listOf("2"), KeyPopupOptions.alternatesFor("w"))
    }

    @Test
    fun nonTopRowLetterWithAccents_hasNoDigit() {
        val c = KeyPopupOptions.alternatesFor("c")
        assertFalse(c.any { it.all { ch -> ch.isDigit() } })
        assertTrue(c.contains("ç"))
    }

    @Test
    fun plainLetter_withNothing_hasNoAlternates() {
        // A bottom-row letter with no accents and no digit.
        assertFalse(KeyPopupOptions.hasAlternates("b"))
        assertTrue(KeyPopupOptions.alternatesFor("b").isEmpty())
    }

    @Test
    fun isCaseInsensitiveOnTheBaseLetter() {
        assertEquals(KeyPopupOptions.alternatesFor("e"), KeyPopupOptions.alternatesFor("E"))
    }

    @Test
    fun period_hasPunctuationAlternates() {
        assertTrue(KeyPopupOptions.alternatesFor(".").contains("?"))
    }
}
