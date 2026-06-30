package dev.varuntej.aura.keyboard

/**
 * The actions offered on the Buddy bar above the keys.
 *
 * [wire] mirrors the backend /keyboard/draft action contract
 * (backend/src/services/keyboard/drafter.py) so the native action set and the
 * server can never silently drift. It is unused by the stub shell but is the
 * value the real request will send once the keyboard is wired to the backend.
 */
enum class BuddyAction(val wire: String, val label: String) {
    REPLY("reply", "Reply as me"),
    CONTINUE("continue", "Continue"),
    REWRITE("rewrite", "Rewrite"),
    GRAMMAR("grammar", "Grammar"),
    TRANSLATE("translate", "Translate");

    companion object {
        /** Left-to-right order the actions appear in on the Buddy bar. */
        val barOrder: List<BuddyAction> = listOf(REPLY, CONTINUE, REWRITE, GRAMMAR, TRANSLATE)
    }
}

/**
 * One tab in the Gboard-style writing-tools panel (single editable preview + "Use this"). Each tab
 * maps onto the existing /keyboard/draft contract: [action] is the wire action, [tone] is the
 * optional free-text tone passed alongside a `rewrite` (the backend accepts any tone string).
 * [needsLanguage] tabs (Translate) first ask for a target language. The Proofread/Rephrase/
 * Professional/Friendly/Emoji set matches the reference screenshot; Reply/Continue/Translate keep
 * Buddy's own superpowers ("reply as me" in your voice) on the same scrollable row.
 */
data class WritingTool(
    val label: String,
    val action: BuddyAction,
    val tone: String?,
    val needsLanguage: Boolean = false,
) {
    companion object {
        val tabs: List<WritingTool> = listOf(
            WritingTool("Proofread", BuddyAction.GRAMMAR, tone = null),
            WritingTool("Rephrase", BuddyAction.REWRITE, tone = null),
            WritingTool("Professional", BuddyAction.REWRITE, tone = "professional"),
            WritingTool("Friendly", BuddyAction.REWRITE, tone = "friendly"),
            WritingTool("Emoji", BuddyAction.REWRITE, tone = "add relevant emojis"),
            WritingTool("Reply as me", BuddyAction.REPLY, tone = null),
            WritingTool("Continue", BuddyAction.CONTINUE, tone = null),
            WritingTool("Translate", BuddyAction.TRANSLATE, tone = null, needsLanguage = true),
        )
    }
}
