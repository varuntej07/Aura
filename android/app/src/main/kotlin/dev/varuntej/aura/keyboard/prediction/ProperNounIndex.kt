package dev.varuntej.aura.keyboard.prediction

/**
 * Maps a known vocab token to the casing the user's profile stores it in, so a proper noun typed
 * in another case ("kcr", "lululemon") can be auto-capitalized on commit to its real form
 * ("KCR", "Lululemon"). Built from the consent-gated vocab hints, which already preserve original
 * casing and dedup by lowercase. Pure and unit-tested; [VocabHintsCache] holds one and rebuilds it
 * whenever the hint set changes.
 */
class ProperNounIndex private constructor(private val byLower: Map<String, String>) {

    /**
     * The stored display form of [word] when it differs from what the user actually typed (a
     * genuine proper noun in another casing). Returns null when the word is unknown, has no
     * alternate casing, or was already typed in its stored form, so ordinary words and
     * already-correct input are left untouched.
     */
    fun displayForm(word: String): String? {
        val stored = byLower[word.lowercase()] ?: return null
        return if (stored != word) stored else null
    }

    companion object {
        val EMPTY = ProperNounIndex(emptyMap())

        /** Build from raw tokens, keeping the first casing seen per lowercase key (matching how the
         *  backend dedups). An all-lowercase token contributes no casing to restore, so it is
         *  skipped; an index with no such tokens collapses to [EMPTY]. */
        fun from(tokens: List<String>): ProperNounIndex {
            if (tokens.isEmpty()) return EMPTY
            val map = HashMap<String, String>(tokens.size)
            for (token in tokens) {
                val lower = token.lowercase()
                if (lower == token) continue // already lowercase: nothing to restore
                map.putIfAbsent(lower, token)
            }
            return if (map.isEmpty()) EMPTY else ProperNounIndex(map)
        }
    }
}
