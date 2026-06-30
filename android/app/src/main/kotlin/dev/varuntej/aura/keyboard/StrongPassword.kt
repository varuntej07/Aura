package dev.varuntej.aura.keyboard

import java.security.SecureRandom

/**
 * Generates a strong random password locally (offline) for the keyboard's password
 * field chip. The value is committed straight into the field and saved by the OS
 * autofill ("Save password?"); it is never sent to our backend or stored by the
 * keyboard. Uses [SecureRandom], guarantees at least one of each character class, and
 * excludes ambiguous glyphs (O/0, l/1/I) so the value is readable if ever shown.
 */
object StrongPassword {

    private const val LOWER = "abcdefghijkmnpqrstuvwxyz"
    private const val UPPER = "ABCDEFGHJKLMNPQRSTUVWXYZ"
    private const val DIGITS = "23456789"
    private const val SYMBOLS = "!@#\$%^&*?-_+="
    private val ALL = LOWER + UPPER + DIGITS + SYMBOLS

    private val random = SecureRandom()

    fun generate(length: Int = 16): String {
        val n = length.coerceIn(12, 64)
        // One guaranteed character from each class, then fill, then shuffle so the
        // guaranteed four are not always in the first positions.
        val chars = ArrayList<Char>(n)
        chars.add(LOWER.pick())
        chars.add(UPPER.pick())
        chars.add(DIGITS.pick())
        chars.add(SYMBOLS.pick())
        while (chars.size < n) chars.add(ALL.pick())
        for (i in chars.indices.reversed()) {
            val j = random.nextInt(i + 1)
            val tmp = chars[i]; chars[i] = chars[j]; chars[j] = tmp
        }
        return String(chars.toCharArray())
    }

    private fun String.pick(): Char = this[random.nextInt(this.length)]
}
