package dev.varuntej.aura.keyboard.prediction

import org.junit.Assert.assertTrue
import org.junit.Test

class KeyboardGeometryTest {
    @Test
    fun neighboringSubstitutionOutranksDistantSubstitution() {
        val neighboring = KeyboardGeometry.proximityScore("test", "rest")
        val distant = KeyboardGeometry.proximityScore("test", "pest")
        assertTrue("neighboring=$neighboring distant=$distant", neighboring > distant)
    }

    @Test
    fun adjacentTransposeAddsEvidence() {
        assertTrue(KeyboardGeometry.proximityScore("teh", "the") > 0)
    }
}
