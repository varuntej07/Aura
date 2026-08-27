package dev.varuntej.aura.keyboard.prediction

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class CachedAutocorrectPolicyTest {
    private val decision = CachedAutocorrect("teh", "the", 4.0)

    @Test
    fun exactCurrentGeneration_isConsumedInConstantTime() {
        assertEquals(decision, consume(generation = 7, active = 7, raw = "teh"))
    }

    @Test
    fun missingStaleOrDifferentWord_keepsTypedTextUnchanged() {
        assertNull(CachedAutocorrectPolicy.consume(null, null, 7, "teh", false))
        assertNull(consume(generation = 6, active = 7, raw = "teh"))
        assertNull(consume(generation = 7, active = 7, raw = "ten"))
    }

    @Test
    fun manualUserCorrection_alwaysWinsOverAutomaticCache() {
        assertNull(consume(generation = 7, active = 7, raw = "teh", manual = true))
    }

    private fun consume(
        generation: Long,
        active: Long,
        raw: String,
        manual: Boolean = false,
    ) = CachedAutocorrectPolicy.consume(decision, generation, active, raw, manual)
}
