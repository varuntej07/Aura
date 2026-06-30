package dev.varuntej.aura.keyboard.prediction

/**
 * The in-progress word, mirrored in memory. Letters are COMMITTED to the field directly per
 * keystroke (so they land instantly with no composing underline, like Gboard); this buffer is the
 * in-memory source of truth for the same word, which is what makes word prediction and
 * autocorrect-on-separator possible without touching the host's committed text on every key.
 *
 * Pure and unit-testable: no Android imports. [BuddyImeService] owns the InputConnection and keeps
 * this buffer in lock-step with what it commits / deletes; this class only tracks the characters.
 * The single reused [StringBuilder] keeps the hot typing path allocation-light.
 *
 * Caret model: characters are appended and deleted at the END of the word only. Any cursor move,
 * selection, or external edit makes this state stale, so the IME drops the word and calls [reset];
 * re-seeding the composer from a word the user tapped back into is a deliberate later enhancement.
 */
class WordComposer {

    private val builder = StringBuilder()

    /** The in-progress word, or "" when nothing is composing. */
    val current: String get() = builder.toString()

    /** True while a word is being composed (the buffer is non-empty). */
    val isComposing: Boolean get() = builder.isNotEmpty()

    /** Append a typed grapheme. The caller passes it already cased (shift applied). */
    fun append(text: String) {
        builder.append(text)
    }

    /**
     * Remove the last character of the composing word. Returns true if a character was
     * removed (the caller should re-render the composing text), false when the buffer was
     * already empty (the caller should perform a real backspace on the field instead).
     */
    fun deleteLast(): Boolean {
        if (builder.isEmpty()) return false
        builder.deleteCharAt(builder.length - 1)
        return true
    }

    /** Drop the in-progress word. Called after the word is finalized, or on a cursor move. */
    fun reset() {
        builder.setLength(0)
    }
}
