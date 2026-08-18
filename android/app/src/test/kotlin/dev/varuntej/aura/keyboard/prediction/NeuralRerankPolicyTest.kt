package dev.varuntej.aura.keyboard.prediction

import org.junit.Assert.assertEquals
import org.junit.Assert.assertSame
import org.junit.Test

class NeuralRerankPolicyTest {
    private val lexical = listOf(
        Suggestion("their", SuggestionSource.BASE),
        Suggestion("there", SuggestionSource.BASE),
        Suggestion("they", SuggestionSource.BASE),
    )

    @Test
    fun unavailableModel_preservesExactLexicalOutput() {
        assertSame(lexical, NeuralRerankPolicy.apply(lexical, null))
    }

    @Test
    fun lexicalWinner_preservesExactOutput() {
        assertSame(lexical, NeuralRerankPolicy.apply(lexical, floatArrayOf(2f, 1f, 0f)))
    }

    @Test
    fun invalidOrLowMarginOutput_failsOpenToLexicalOrder() {
        assertSame(lexical, NeuralRerankPolicy.apply(lexical, floatArrayOf(0.1f, Float.NaN, 0f)))
        assertSame(lexical, NeuralRerankPolicy.apply(lexical, floatArrayOf(0.1f, 0.2f, 0f)))
    }

    @Test
    fun confidentOutput_onlyReordersExistingBoundedCandidates() {
        val result = NeuralRerankPolicy.apply(lexical, floatArrayOf(0f, 0.5f, 0.1f))
        assertEquals(listOf("there", "their", "they"), result.map(Suggestion::word))
        assertEquals(lexical.toSet(), result.toSet())
    }
}
