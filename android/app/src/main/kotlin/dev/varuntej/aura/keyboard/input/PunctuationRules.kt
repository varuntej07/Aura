package dev.varuntej.aura.keyboard.input

/**
 * Pure recognizers for the two "invisible" spacing conveniences keyboard users expect, kept out of
 * BuddyImeService so the window and context rules are deterministic and unit-tested (mirrors
 * [ShiftState] and [SentenceCapitalizer]). The IME owns the InputConnection and applies the edits.
 */

/**
 * Double-space to period: two spaces in quick succession after a word become ". ". A separate
 * object so the window plus context rules are testable without an InputConnection.
 */
object DoubleSpacePeriod {

    /**
     * Whether a SPACE press should turn the preceding space into ". ". True only when the char
     * before the cursor is a space, the char before THAT is a word character (so a lone or leading
     * space, and a space after punctuation, are left alone), and the two taps fell within
     * [windowMs]. [textBeforeCursor] is the last couple of characters before the cursor.
     */
    fun shouldConvert(textBeforeCursor: CharSequence?, elapsedMs: Long, windowMs: Long): Boolean {
        if (textBeforeCursor == null || textBeforeCursor.length < 2) return false
        if (elapsedMs < 0 || elapsedMs > windowMs) return false
        val last = textBeforeCursor[textBeforeCursor.length - 1]
        val beforeLast = textBeforeCursor[textBeforeCursor.length - 2]
        return last == ' ' && beforeLast.isLetterOrDigit()
    }
}

/**
 * Auto-space after punctuation: a sentence or clause mark gets a trailing space when more text
 * follows on the same line, so the user does not have to add it by hand (the common case is
 * inserting punctuation back into existing text or after a paste).
 */
object PunctuationSpacer {

    private val AUTO_SPACE_AFTER = setOf('.', ',', '!', '?', ':', ';')

    /** Whether a space should be inserted after [punctuation], given the [nextChar] right after the
     *  cursor. Only for the known marks, and only when there is following text on the line that is
     *  not already a space (so end-of-field typing, the usual case, is untouched). */
    fun shouldInsertSpace(punctuation: Char, nextChar: Char?): Boolean {
        if (punctuation !in AUTO_SPACE_AFTER) return false
        return nextChar != null && nextChar != ' ' && nextChar != '\n'
    }
}
