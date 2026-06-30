package dev.varuntej.aura.keyboard.input

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/** Pure-JVM coverage for the backspace auto-repeat acceleration schedule. */
class BackspaceRepeatTest {

    @Test
    fun firstRepeatIsTheStartDelay_andItAccelerates() {
        val t0 = BackspaceRepeat.delayForTick(0)
        val t1 = BackspaceRepeat.delayForTick(1)
        val t5 = BackspaceRepeat.delayForTick(5)
        assertTrue(t1 < t0)
        assertTrue(t5 < t1)
    }

    @Test
    fun neverDropsBelowTheFloor() {
        val late = BackspaceRepeat.delayForTick(1000)
        assertEquals(BackspaceRepeat.delayForTick(1000), late)
        assertTrue(late >= 38L)
    }

    @Test
    fun isMonotonicNonIncreasing() {
        var prev = Long.MAX_VALUE
        for (tick in 0..50) {
            val d = BackspaceRepeat.delayForTick(tick)
            assertTrue("tick $tick not <= prev", d <= prev)
            prev = d
        }
    }
}
