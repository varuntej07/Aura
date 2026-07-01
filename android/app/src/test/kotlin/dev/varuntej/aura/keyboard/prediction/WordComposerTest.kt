package dev.varuntej.aura.keyboard.prediction

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/** Pure-JVM coverage for the in-progress word buffer behind the composing-text pipeline. */
class WordComposerTest {

    @Test
    fun startsEmpty() {
        val c = WordComposer()
        assertEquals("", c.current)
        assertFalse(c.isComposing)
    }

    @Test
    fun appendBuildsTheWord() {
        val c = WordComposer()
        c.append("h")
        c.append("i")
        assertEquals("hi", c.current)
        assertTrue(c.isComposing)
    }

    @Test
    fun appendKeepsTheCaseTheCallerPassed() {
        // The IME applies shift before appending, so the composer never re-cases.
        val c = WordComposer()
        c.append("H")
        c.append("e")
        c.append("y")
        assertEquals("Hey", c.current)
    }

    @Test
    fun deleteLastRemovesOneChar_andReportsTrue() {
        val c = WordComposer()
        c.append("c")
        c.append("a")
        c.append("t")
        assertTrue(c.deleteLast())
        assertEquals("ca", c.current)
    }

    @Test
    fun deleteLastOnEmptyReportsFalse() {
        // False tells the IME to do a real backspace on the field instead.
        val c = WordComposer()
        assertFalse(c.deleteLast())
        assertEquals("", c.current)
    }

    @Test
    fun deletingTheLastCharEndsComposing() {
        val c = WordComposer()
        c.append("a")
        assertTrue(c.deleteLast())
        assertFalse(c.isComposing)
        assertEquals("", c.current)
    }

    @Test
    fun resetClearsTheWord() {
        val c = WordComposer()
        c.append("h")
        c.append("e")
        c.append("l")
        c.append("l")
        c.append("o")
        c.reset()
        assertEquals("", c.current)
        assertFalse(c.isComposing)
    }

    @Test
    fun reusableAfterReset() {
        val c = WordComposer()
        c.append("o")
        c.append("n")
        c.append("e")
        c.reset()
        c.append("t")
        c.append("w")
        c.append("o")
        assertEquals("two", c.current)
    }
}
