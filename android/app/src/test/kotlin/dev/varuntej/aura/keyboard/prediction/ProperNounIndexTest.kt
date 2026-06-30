package dev.varuntej.aura.keyboard.prediction

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

/** Pure-JVM coverage for proper-noun casing restoration. */
class ProperNounIndexTest {

    private val index = ProperNounIndex.from(listOf("KCR", "Lululemon", "running", "iPhone"))

    @Test
    fun lowercaseInput_upgradesToStoredCasing() {
        assertEquals("KCR", index.displayForm("kcr"))
        assertEquals("Lululemon", index.displayForm("lululemon"))
        assertEquals("iPhone", index.displayForm("iphone"))
    }

    @Test
    fun mixedCaseInput_upgradesToStoredCasing() {
        assertEquals("KCR", index.displayForm("Kcr"))
    }

    @Test
    fun alreadyCorrectInput_returnsNull() {
        assertNull(index.displayForm("KCR"))
        assertNull(index.displayForm("Lululemon"))
    }

    @Test
    fun lowercaseOnlyToken_contributesNoUpgrade() {
        // "running" has no alternate casing in the source, so it is never returned as a proper noun.
        assertNull(index.displayForm("running"))
        assertNull(index.displayForm("Running"))
    }

    @Test
    fun unknownWord_returnsNull() {
        assertNull(index.displayForm("hyderabad"))
    }

    @Test
    fun empty_isAlwaysNull() {
        assertNull(ProperNounIndex.EMPTY.displayForm("anything"))
        assertNull(ProperNounIndex.from(emptyList()).displayForm("kcr"))
    }
}
