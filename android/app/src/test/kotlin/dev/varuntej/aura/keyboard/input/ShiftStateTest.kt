package dev.varuntej.aura.keyboard.input

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/** Pure-JVM coverage for the shift state machine. */
class ShiftStateTest {

    @Test
    fun startsNone() {
        val s = ShiftState()
        assertEquals(ShiftMode.NONE, s.mode)
        assertFalse(s.isUpper)
    }

    @Test
    fun singleTap_togglesOneShotShift() {
        val s = ShiftState()
        s.onShiftTap(doubleTap = false)
        assertEquals(ShiftMode.SHIFTED, s.mode)
        assertTrue(s.isUpper)
        s.onShiftTap(doubleTap = false)
        assertEquals(ShiftMode.NONE, s.mode)
    }

    @Test
    fun committingALetter_consumesOneShotShift() {
        val s = ShiftState()
        s.onShiftTap(doubleTap = false)
        s.onTextCommitted()
        assertEquals(ShiftMode.NONE, s.mode) // only the first letter is capitalized
    }

    @Test
    fun doubleTap_latchesCapsLock_andSurvivesCommits() {
        val s = ShiftState()
        s.onShiftTap(doubleTap = true)
        assertEquals(ShiftMode.CAPS_LOCK, s.mode)
        assertTrue(s.isCapsLock)
        s.onTextCommitted()
        assertEquals(ShiftMode.CAPS_LOCK, s.mode) // caps lock is not consumed
        assertTrue(s.isUpper)
    }

    @Test
    fun singleTap_clearsCapsLock() {
        val s = ShiftState()
        s.onShiftTap(doubleTap = true)
        s.onShiftTap(doubleTap = false)
        assertEquals(ShiftMode.NONE, s.mode)
    }

    @Test
    fun autoCap_setsShift_butNeverOverridesCapsLock() {
        val s = ShiftState()
        s.applyAutoCap(shouldCapitalize = true)
        assertEquals(ShiftMode.SHIFTED, s.mode)
        s.applyAutoCap(shouldCapitalize = false)
        assertEquals(ShiftMode.NONE, s.mode)

        s.onShiftTap(doubleTap = true) // caps lock on
        s.applyAutoCap(shouldCapitalize = false)
        assertEquals(ShiftMode.CAPS_LOCK, s.mode) // untouched
    }

    @Test
    fun reset_goesBackToNone() {
        val s = ShiftState()
        s.onShiftTap(doubleTap = true)
        s.reset()
        assertEquals(ShiftMode.NONE, s.mode)
    }
}
