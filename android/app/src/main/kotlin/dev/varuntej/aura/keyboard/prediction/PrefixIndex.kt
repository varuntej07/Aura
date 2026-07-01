package dev.varuntej.aura.keyboard.prediction

/** A dictionary word paired with its corpus frequency (higher = more common). */
data class WordCandidate(val word: String, val frequency: Int)

/**
 * An immutable, frequency-ranked prefix index over a word list.
 *
 * Backed by two parallel alphabetically-sorted arrays (words + their frequencies), NOT a node
 * trie: this is compact (no per-node objects), cache-friendly, and lets a prefix lookup be a
 * binary search to the first match plus a short forward scan that keeps only a fixed top-K by
 * frequency. The hot path allocates nothing beyond the small result list, so it stays well
 * under a frame even for a one-letter prefix. Pure JVM, unit-tested.
 *
 * Matching is case-insensitive: a lowercased match key is stored for every word and queries are
 * lowercased, while the original casing is kept separately and surfaced in completions (so a
 * capitalized term like "KCR" or a Settings-dictionary "Aura" is matched by "kcr"/"aura" yet
 * offered in its real form). Exact-membership lookups ([contains] / [frequencyOf]) back the
 * spell-check "known word" set, so a known capitalized word is never flagged or autocorrected.
 */
class PrefixIndex private constructor(
    private val keys: Array<String>,         // lowercased match keys, sorted ascending
    private val displayForms: Array<String>, // parallel to keys: the original casing to surface
    private val freqs: IntArray,             // parallel to keys
) {

    val size: Int get() = keys.size

    /**
     * Up to [limit] words beginning with [prefix], highest frequency first. The prefix is
     * lowercased to match the stored words; an empty prefix (or non-positive limit) returns
     * nothing, since "everything" is not a useful completion set.
     */
    fun completions(prefix: String, limit: Int): List<WordCandidate> {
        if (prefix.isEmpty() || limit <= 0 || keys.isEmpty()) return emptyList()
        val p = prefix.lowercase()
        val topWords = arrayOfNulls<String>(limit)
        val topFreqs = IntArray(limit)
        var filled = 0
        var i = lowerBound(p)
        while (i < keys.size && keys[i].startsWith(p)) {
            // Rank on the match key's frequency but surface the original casing ([displayForms]).
            filled = offer(topWords, topFreqs, filled, limit, displayForms[i], freqs[i])
            i++
        }
        val out = ArrayList<WordCandidate>(filled)
        for (j in 0 until filled) out.add(WordCandidate(topWords[j]!!, topFreqs[j]))
        return out
    }

    /** Whether [word] is in the dictionary (exact, case-insensitive). */
    fun contains(word: String): Boolean = indexOfWord(word.lowercase()) >= 0

    /** The frequency of [word], or 0 if it is not in the dictionary. */
    fun frequencyOf(word: String): Int {
        val i = indexOfWord(word.lowercase())
        return if (i >= 0) freqs[i] else 0
    }

    /** First index whose match key is >= [key] (the start of the matching prefix range). */
    private fun lowerBound(key: String): Int {
        var lo = 0
        var hi = keys.size
        while (lo < hi) {
            val mid = (lo + hi) ushr 1
            if (keys[mid] < key) lo = mid + 1 else hi = mid
        }
        return lo
    }

    /** Index of the exact match [key], or -1. */
    private fun indexOfWord(key: String): Int {
        var lo = 0
        var hi = keys.size - 1
        while (lo <= hi) {
            val mid = (lo + hi) ushr 1
            val cmp = keys[mid].compareTo(key)
            when {
                cmp < 0 -> lo = mid + 1
                cmp > 0 -> hi = mid - 1
                else -> return mid
            }
        }
        return -1
    }

    companion object {
        fun from(entries: List<WordCandidate>): PrefixIndex {
            // Sort by the lowercased match key so binary search is case-insensitive, but keep the
            // original casing as the display form (entries are already lowercase for the en_50k base
            // asset, so this is a no-op there and only matters for mixed-case provider/vocab words).
            val sorted = entries.sortedBy { it.word.lowercase() }
            val keys = Array(sorted.size) { sorted[it].word.lowercase() }
            val displayForms = Array(sorted.size) { sorted[it].word }
            val freqs = IntArray(sorted.size) { sorted[it].frequency }
            return PrefixIndex(keys, displayForms, freqs)
        }

        /**
         * Insert (word, freq) into the top-K arrays kept in descending frequency order, in
         * place, and return the new filled count. Capacity is tiny (~3), so the linear shift
         * is cheaper than any heap. Equal frequencies keep the earlier (alphabetically smaller)
         * word, because the caller scans words ascending.
         */
        private fun offer(
            topWords: Array<String?>,
            topFreqs: IntArray,
            filled: Int,
            cap: Int,
            word: String,
            freq: Int,
        ): Int {
            if (filled >= cap && freq <= topFreqs[cap - 1]) return filled
            var pos = if (filled < cap) filled else cap - 1
            while (pos > 0 && topFreqs[pos - 1] < freq) {
                topWords[pos] = topWords[pos - 1]
                topFreqs[pos] = topFreqs[pos - 1]
                pos--
            }
            topWords[pos] = word
            topFreqs[pos] = freq
            return if (filled < cap) filled + 1 else filled
        }
    }
}
