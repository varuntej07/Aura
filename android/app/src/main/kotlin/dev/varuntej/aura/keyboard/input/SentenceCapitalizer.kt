package dev.varuntej.aura.keyboard.input

/**
 * Decides whether the next letter should be auto-capitalized, from the text before the cursor.
 * Pure and unit-tested. Capitalize at the very start of the field, at the start of a new line,
 * and after a sentence-ending `. ? !` (trailing spaces are ignored so it fires after ". ").
 */
object SentenceCapitalizer {

    fun shouldCapitalize(textBeforeCursor: CharSequence?): Boolean {
        if (textBeforeCursor.isNullOrEmpty()) return true
        var i = textBeforeCursor.length - 1
        while (i >= 0 && (textBeforeCursor[i] == ' ' || textBeforeCursor[i] == '\t')) i--
        if (i < 0) return true // only whitespace before the cursor -> still a start
        return when (textBeforeCursor[i]) {
            '.', '?', '!', '\n' -> true
            else -> false
        }
    }
}
