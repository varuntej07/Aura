package dev.varuntej.aura.keyboard.prediction

/**
 * Decides whether to autocorrect the composing word when the user hits a separator. Pure and
 * unit-tested.
 *
 * Conservative on purpose: it only replaces a word that is actually misspelled AND has a known
 * single-edit correction, and it preserves the word's case pattern. A correctly-spelled word, a
 * word the user knows (learned / system / a name), and a misspelling with no confident correction
 * are all left exactly as typed, so the keyboard never "corrects" intentional text into something
 * wrong. The IME records the [Decision.Replace] so it can offer a one-tap undo.
 */
class Autocorrector(private val spellChecker: SpellChecker) {

    sealed class Decision {
        /** Leave the word as the user typed it. */
        object Keep : Decision()

        /** Replace [original] with [corrected] (already cased to match the original). */
        data class Replace(val original: String, val corrected: String) : Decision()
    }

    fun onSeparator(word: String): Decision {
        if (!spellChecker.isMisspelled(word)) return Decision.Keep
        // Edit distance 1 only: this runs synchronously on the main thread when the user hits a
        // separator, so it must never trigger the expensive two-edit BFS. A two-edit correction is
        // still offered (and tappable) in the strip via the off-thread pass; it is just not
        // auto-applied. Conservative by design: a distance-2 guess is too risky to apply silently.
        val best = spellChecker.corrections(word, 1, maxEditDistance = 1).firstOrNull()
            ?: return Decision.Keep
        val cased = applyCasePattern(word, best)
        return if (cased == word) Decision.Keep else Decision.Replace(word, cased)
    }

    companion object {
        /** Re-case [targetLower] to follow [source]'s pattern: ALL CAPS, Title, or lower. */
        fun applyCasePattern(source: String, targetLower: String): String = when {
            source.isEmpty() -> targetLower
            source.length > 1 && source.all { it.isUpperCase() } -> targetLower.uppercase()
            source.first().isUpperCase() -> targetLower.replaceFirstChar { it.uppercaseChar() }
            else -> targetLower
        }
    }
}
