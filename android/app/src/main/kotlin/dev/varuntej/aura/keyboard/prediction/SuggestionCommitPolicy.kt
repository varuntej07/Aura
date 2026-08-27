package dev.varuntej.aura.keyboard.prediction

internal data class SuggestionCommitPlan(
    val deleteBeforeCursor: Int,
    val committedText: String,
    val cursorDelta: Int,
)

/** Pure edit plan applied only after BuddyImeService verifies the local partial against the host. */
internal object SuggestionCommitPolicy {
    fun plan(partial: String, acceptedWord: String): SuggestionCommitPlan {
        require(acceptedWord.isNotBlank())
        val committed = "$acceptedWord "
        return SuggestionCommitPlan(
            deleteBeforeCursor = partial.length,
            committedText = committed,
            cursorDelta = committed.length - partial.length,
        )
    }
}
