package dev.varuntej.aura.keyboard.input

/**
 * The long-press alternates for a key: accented variants for letters, the paired digit for the
 * top QWERTY row (Gboard-style q->1 … p->0), and a few punctuation alternates. Pure and
 * unit-tested; the touch handler shows these in a popup and commits the chosen one.
 *
 * Returned lowercase/base; the IME applies shift casing on commit (and for display), so an accent
 * is committed uppercase when shift is active, just like a normal letter.
 */
object KeyPopupOptions {

    // The digit each top-row letter long-presses to (the numbers row without a mode switch).
    private val topRowDigit = mapOf(
        "q" to "1", "w" to "2", "e" to "3", "r" to "4", "t" to "5",
        "y" to "6", "u" to "7", "i" to "8", "o" to "9", "p" to "0",
    )

    // Accented variants per letter (common Latin diacritics).
    private val accents = mapOf(
        "a" to listOf("à", "á", "â", "ä", "æ", "ã", "å", "ā"),
        "e" to listOf("è", "é", "ê", "ë", "ē", "ė", "ę"),
        "i" to listOf("î", "ï", "í", "ī", "į", "ì"),
        "o" to listOf("ô", "ö", "ò", "ó", "œ", "ø", "ō", "õ"),
        "u" to listOf("û", "ü", "ù", "ú", "ū"),
        "c" to listOf("ç", "ć", "č"),
        "n" to listOf("ñ", "ń"),
        "s" to listOf("ß", "ś", "š"),
        "y" to listOf("ÿ", "ý"),
        "z" to listOf("ž", "ź", "ż"),
        "g" to listOf("ğ"),
        "l" to listOf("ł"),
    )

    // Punctuation alternates, keyed by the exact character.
    private val punctuation = mapOf(
        "." to listOf(",", "?", "!", ";", ":", "'", "\"", "-"),
        "," to listOf("'", "\""),
    )

    /** The long-press alternates for [output], in display order (digit first, then accents).
     *  Empty when the key has none. */
    fun alternatesFor(output: String): List<String> {
        val lower = output.lowercase()
        val result = ArrayList<String>()
        topRowDigit[lower]?.let { result.add(it) }
        accents[lower]?.let { result.addAll(it) }
        punctuation[output]?.let { result.addAll(it) }
        return result
    }

    /** Whether [output] has any long-press alternates (so the handler arms a long-press). */
    fun hasAlternates(output: String): Boolean = alternatesFor(output).isNotEmpty()
}
