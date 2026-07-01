package dev.varuntej.aura.keyboard.input

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/** Pure-JVM coverage for the double-space-to-period and auto-space-after-punctuation rules. */
class PunctuationRulesTest {

    private val window = 500L

    @Test
    fun doubleSpace_convertsAfterAWordWithinTheWindow() {
        assertTrue(DoubleSpacePeriod.shouldConvert("hi ", elapsedMs = 200, windowMs = window))
        assertTrue(DoubleSpacePeriod.shouldConvert("hi ", elapsedMs = 500, windowMs = window)) // at the edge
    }

    @Test
    fun doubleSpace_ignoredOutsideTheWindow() {
        assertFalse(DoubleSpacePeriod.shouldConvert("hi ", elapsedMs = 800, windowMs = window))
        assertFalse(DoubleSpacePeriod.shouldConvert("hi ", elapsedMs = -1, windowMs = window))
    }

    @Test
    fun doubleSpace_ignoredWhenNotPrecededByAWord() {
        assertFalse(DoubleSpacePeriod.shouldConvert("hi. ", 100, window)) // after a period
        assertFalse(DoubleSpacePeriod.shouldConvert("  ", 100, window))   // a double / leading space
        assertFalse(DoubleSpacePeriod.shouldConvert(" ", 100, window))    // too short to inspect
        assertFalse(DoubleSpacePeriod.shouldConvert("hi", 100, window))   // no trailing space
        assertFalse(DoubleSpacePeriod.shouldConvert(null, 100, window))
    }

    @Test
    fun autoSpace_insertsAfterPunctuationWhenTextFollows() {
        assertTrue(PunctuationSpacer.shouldInsertSpace('.', 'w'))
        assertTrue(PunctuationSpacer.shouldInsertSpace(',', 'a'))
        assertTrue(PunctuationSpacer.shouldInsertSpace('?', 'b'))
    }

    @Test
    fun autoSpace_skippedAtEndOfLineOrField_orWhenAlreadySpaced() {
        assertFalse(PunctuationSpacer.shouldInsertSpace('.', null))
        assertFalse(PunctuationSpacer.shouldInsertSpace('.', '\n'))
        assertFalse(PunctuationSpacer.shouldInsertSpace('.', ' '))
    }

    @Test
    fun autoSpace_skippedForNonPunctuation() {
        assertFalse(PunctuationSpacer.shouldInsertSpace('a', 'b'))
        assertFalse(PunctuationSpacer.shouldInsertSpace('-', 'b'))
    }
}
