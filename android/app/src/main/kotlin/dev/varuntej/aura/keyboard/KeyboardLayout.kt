package dev.varuntej.aura.keyboard

/** A function (non-character) key. */
enum class FuncType { SHIFT, BACKSPACE, SYMBOLS, LETTERS, GLOBE, EMOJI, SPACE, ENTER }

/** One key on the keyboard: either a character to commit or a function key. */
sealed class Key {
    /** Commits [output]. For single letters, shift uppercases it at commit time. */
    data class Char(val output: String) : Key()

    /** A function key rendered with [label]. */
    data class Func(val type: FuncType, val label: String) : Key()
}

/**
 * The single source of truth for the keyboard's key layout. Pure data: the view
 * objects are built from these rows by BuddyImeService (which holds the Context).
 *
 * Two pages: the QWERTY letters page and a single symbols/numbers page reachable
 * via the ?123 / ABC toggle. Numeric layouts retain an explicit globe key.
 */
object KeyboardLayout {

    private fun chars(vararg cs: String): List<Key> = cs.map { Key.Char(it) }

    // Gboard-style bottom row from the approved reference, with Aura's existing key behavior.
    private val bottomLetters: List<Key> = listOf(
        Key.Func(FuncType.SYMBOLS, "?123"),
        Key.Char(","),
        Key.Func(FuncType.EMOJI, "☺"),
        Key.Func(FuncType.SPACE, "English"),
        Key.Char("."),
        Key.Func(FuncType.ENTER, "↵"), // return arrow
    )

    private val bottomSymbols: List<Key> = listOf(
        Key.Func(FuncType.LETTERS, "ABC"),
        Key.Char(","),
        Key.Func(FuncType.EMOJI, "☺"),
        Key.Func(FuncType.SPACE, "English"),
        Key.Char("."),
        Key.Func(FuncType.ENTER, "↵"),
    )

    // Email/url address fields swap the comma and emoji slots for characters those fields
    // actually need (@ and .com, or / and .com).
    private val bottomEmail: List<Key> = listOf(
        Key.Func(FuncType.SYMBOLS, "?123"),
        Key.Char("@"),
        Key.Func(FuncType.SPACE, "English"),
        Key.Char("."),
        Key.Char(".com"),
        Key.Func(FuncType.ENTER, "↵"),
    )

    private val bottomUrl: List<Key> = listOf(
        Key.Func(FuncType.SYMBOLS, "?123"),
        Key.Char("/"),
        Key.Func(FuncType.SPACE, "English"),
        Key.Char("."),
        Key.Char(".com"),
        Key.Func(FuncType.ENTER, "↵"),
    )

    // The shared QWERTY top three rows; the bottom row varies per field (plain,
    // email, url).
    private val letterRowsTop: List<List<Key>> = listOf(
        chars("q", "w", "e", "r", "t", "y", "u", "i", "o", "p"),
        chars("a", "s", "d", "f", "g", "h", "j", "k", "l"),
        listOf(Key.Func(FuncType.SHIFT, "⇧")) + // shift
            chars("z", "x", "c", "v", "b", "n", "m") +
            Key.Func(FuncType.BACKSPACE, "⌫"), // backspace
    )

    val lettersRows: List<List<Key>> = letterRowsTop + listOf(bottomLetters)
    val emailRows: List<List<Key>> = letterRowsTop + listOf(bottomEmail)
    val urlRows: List<List<Key>> = letterRowsTop + listOf(bottomUrl)

    val symbolsRows: List<List<Key>> = listOf(
        chars("1", "2", "3", "4", "5", "6", "7", "8", "9", "0"),
        chars("@", "#", "$", "_", "&", "-", "+", "(", ")", "/"),
        chars("*", "\"", "'", ":", ";", "!", "?") +
            Key.Func(FuncType.BACKSPACE, "⌫"),
        bottomSymbols,
    )

    // Number field: a calculator-style pad with decimal/minus, plus globe, space, and enter.
    val numericRows: List<List<Key>> = listOf(
        chars("1", "2", "3"),
        chars("4", "5", "6"),
        chars("7", "8", "9"),
        listOf(Key.Char("-"), Key.Char("0"), Key.Char("."), Key.Func(FuncType.BACKSPACE, "⌫")),
        listOf(Key.Func(FuncType.GLOBE, "🌐"), Key.Func(FuncType.SPACE, "English"), Key.Func(FuncType.ENTER, "↵")),
    )

    // Phone field: a dialpad with the phone glyphs (+ * #).
    val phoneRows: List<List<Key>> = listOf(
        chars("1", "2", "3"),
        chars("4", "5", "6"),
        chars("7", "8", "9"),
        listOf(Key.Char("*"), Key.Char("0"), Key.Char("#"), Key.Func(FuncType.BACKSPACE, "⌫")),
        listOf(Key.Char("+"), Key.Func(FuncType.GLOBE, "🌐"), Key.Func(FuncType.SPACE, "English"), Key.Func(FuncType.ENTER, "↵")),
    )

    // Numeric password / PIN: the tightest possible pad. No decimal, no minus, no
    // space watermark; globe + backspace + enter keep it escapable.
    val pinRows: List<List<Key>> = listOf(
        chars("1", "2", "3"),
        chars("4", "5", "6"),
        chars("7", "8", "9"),
        listOf(Key.Func(FuncType.GLOBE, "🌐"), Key.Char("0"), Key.Func(FuncType.BACKSPACE, "⌫"), Key.Func(FuncType.ENTER, "↵")),
    )
}
