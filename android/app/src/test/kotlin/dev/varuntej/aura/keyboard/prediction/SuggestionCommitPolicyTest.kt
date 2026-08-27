package dev.varuntej.aura.keyboard.prediction

import org.junit.Assert.assertEquals
import org.junit.Test

class SuggestionCommitPolicyTest {
    @Test
    fun currentWordSuggestion_replacesPartialAndMovesCursorByNetDelta() {
        assertEquals(
            SuggestionCommitPlan(3, "hello ", 3),
            SuggestionCommitPolicy.plan("hel", "hello"),
        )
    }

    @Test
    fun nextWordSuggestion_insertsWithoutDeleting() {
        assertEquals(
            SuggestionCommitPlan(0, "world ", 6),
            SuggestionCommitPolicy.plan("", "world"),
        )
    }
}
