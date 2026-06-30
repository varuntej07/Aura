package dev.varuntej.aura.keyboard

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/** Pure-JVM coverage for the offline strong-password generator. */
class StrongPasswordTest {

    @Test
    fun generate_hasRequestedLength() {
        assertEquals(16, StrongPassword.generate().length)
        assertEquals(20, StrongPassword.generate(20).length)
    }

    @Test
    fun generate_clampsLength() {
        assertEquals(12, StrongPassword.generate(4).length)    // floor 12
        assertEquals(64, StrongPassword.generate(999).length)  // ceiling 64
    }

    @Test
    fun generate_hasOneOfEachClass() {
        repeat(300) {
            val pw = StrongPassword.generate()
            assertTrue("missing lowercase", pw.any { it in 'a'..'z' })
            assertTrue("missing uppercase", pw.any { it in 'A'..'Z' })
            assertTrue("missing digit", pw.any { it in '0'..'9' })
            assertTrue("missing symbol", pw.any { !it.isLetterOrDigit() })
        }
    }

    @Test
    fun generate_excludesAmbiguousChars() {
        val ambiguous = setOf('O', '0', 'l', '1', 'I')
        repeat(300) {
            for (c in StrongPassword.generate()) {
                assertTrue("ambiguous char $c", c !in ambiguous)
            }
        }
    }

    @Test
    fun generate_isRandom() {
        assertNotEquals(StrongPassword.generate(), StrongPassword.generate())
    }
}
