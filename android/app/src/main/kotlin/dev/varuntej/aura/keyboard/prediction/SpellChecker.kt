package dev.varuntej.aura.keyboard.prediction

/**
 * The dictionaries a spell check consults, as a tiny interface so [SpellChecker] stays pure and
 * unit-testable. The IME backs it with base ∪ personal ∪ system (∪ vocab), so a word the user
 * knows (a learned word, a system-dictionary word, one of their own people/topic terms) is never
 * treated as a misspelling.
 */
interface WordSource {
    /** Whether [word] (already lowercase) is a known word in any source. */
    fun isKnown(word: String): Boolean

    /** A ranking signal for [word] (base-dictionary frequency, 0 if unknown). */
    fun frequencyOf(word: String): Int
}

/**
 * On-device spell check. A word is misspelled when it is long enough, all letters, and not known
 * to any [WordSource]. Corrections are generated with the classic edit-distance approach (Norvig's
 * corrector): every delete / transpose / replace / insert of the word, kept if it is a known word,
 * ranked by frequency. Pure and unit-tested.
 *
 * Tiered by edit distance. Single edits (a dropped, added, swapped, or wrong letter, ~54·length
 * candidates) cover the overwhelming majority of typos. Only when no known word is one edit away
 * does it fall back to edit distance 2 ("definately" -> "definitely", "accomodate" ->
 * "accommodate"), bounded by [EDITS2_BRANCH_CAP] so the candidate set stays a few thousand. The
 * two-edit pass is the expensive one, so the IME runs all of this on its prediction thread, off
 * the main thread, deferred until typing pauses.
 */
class SpellChecker(private val source: WordSource) {

    /** Whether [word] should be flagged / is eligible for autocorrect. */
    fun isMisspelled(word: String): Boolean {
        if (word.length < MIN_LENGTH) return false
        val lower = word.lowercase()
        if (!lower.all { it in 'a'..'z' }) return false
        return !source.isKnown(lower)
    }

    /**
     * Up to [limit] known corrections of [word], most frequent first (lowercase). Edit distance 1
     * first, then 2 as a fallback when [maxEditDistance] >= 2.
     *
     * [maxEditDistance] caps how hard this works. The default (2) is for the off-thread suggestion
     * strip, which can afford the expensive two-edit pass. The synchronous autocorrect-on-separator
     * path passes 1 so it never runs the two-edit BFS on the main thread (that pass enumerates tens
     * of thousands of candidates and would blow the keystroke budget); a two-edit fix still reaches
     * the user as a tappable strip suggestion, it is just never auto-applied on the space key.
     */
    fun corrections(word: String, limit: Int, maxEditDistance: Int = 2): List<String> {
        if (limit <= 0 || word.length < MIN_LENGTH) return emptyList()
        val lower = word.lowercase()
        if (!lower.all { it in 'a'..'z' }) return emptyList()

        // Edit distance 1 first: the cheap, overwhelmingly-common case.
        val edit1 = LinkedHashSet<String>()
        for (candidate in edits1(lower)) {
            if (candidate != lower && source.isKnown(candidate)) edit1.add(candidate)
        }
        if (edit1.isNotEmpty() || maxEditDistance < 2) {
            return edit1.sortedByDescending { source.frequencyOf(it) }.take(limit)
        }

        // Edit distance 2 fallback: only when nothing is one edit away. Two hops over the edit
        // graph (edits2 = edits1 of each edits1), with the first hop capped so the candidate set
        // stays a few thousand rather than ~(54·length)².
        val edit2 = LinkedHashSet<String>()
        for (once in edits1(lower).take(EDITS2_BRANCH_CAP)) {
            for (twice in edits1(once)) {
                if (twice != lower && source.isKnown(twice)) edit2.add(twice)
            }
        }
        return edit2.sortedByDescending { source.frequencyOf(it) }.take(limit)
    }

    companion object {
        private const val MIN_LENGTH = 3
        private const val ALPHABET = "abcdefghijklmnopqrstuvwxyz"

        /** First-hop cap for the two-edit fallback: at most this many edit-distance-1 strings are
         *  expanded a second time, keeping the worst case ~EDITS2_BRANCH_CAP·54·length candidates. */
        private const val EDITS2_BRANCH_CAP = 200

        /** Every string one edit (delete / transpose / replace / insert) away from [word]. */
        fun edits1(word: String): Sequence<String> = sequence {
            for (i in 0..word.length) {
                if (i < word.length) {
                    // delete the char at i
                    yield(word.substring(0, i) + word.substring(i + 1))
                    // replace the char at i
                    for (c in ALPHABET) yield(word.substring(0, i) + c + word.substring(i + 1))
                }
                if (i < word.length - 1) {
                    // transpose i and i+1
                    yield(word.substring(0, i) + word[i + 1] + word[i] + word.substring(i + 2))
                }
                // insert before i
                for (c in ALPHABET) yield(word.substring(0, i) + c + word.substring(i))
            }
        }
    }
}
