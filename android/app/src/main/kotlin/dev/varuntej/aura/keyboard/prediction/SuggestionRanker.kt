package dev.varuntej.aura.keyboard.prediction

/** Where a ranked suggestion came from (drives a subtle styling cue and analytics later). */
enum class SuggestionSource(val priority: Int) {
    BASE(0),
    PERSONAL(1),
    VOCAB(2),
    // A spell correction for a misspelled word; the strip's accented autocorrect target. Built
    // directly by the IME (not passed to rank()), so it does not affect completion ranking.
    CORRECTION(3),
    // A predicted next word shown after a space. Also built directly by the IME (not passed to
    // rank()), so its priority is unused for ordering; it exists only to tag the strip chips.
    NEXT_WORD(4),
}

/** One ranked suggestion shown in the strip. */
data class Suggestion(val word: String, val source: SuggestionSource)

/**
 * Merges prefix-matched candidates from the three on-device sources into the top-N strip
 * suggestions. Pure and unit-tested.
 *
 * Each candidate is already prefix-filtered by its source, so the ranker needs no prefix. The
 * ordering is TIERED, by design and intent: source priority is the primary key, frequency only
 * the tiebreaker within a tier. So one of the user's own people/topic terms (vocab) wins its
 * prefix, then a word the user actually types (personal), then a generic dictionary word (base) —
 * a friend's or interest's name is never shoved aside by a common word. Comparing frequencies
 * across sources would be meaningless anyway (base = corpus counts in the millions; personal = a
 * few dozen uses; vocab = no real count), which is exactly why frequency is confined within a tier.
 *
 * In milestone M2 only [base] is populated; [personal] (M3) and [vocab] (M8) plug into the same
 * call without changing it.
 */
object SuggestionRanker {

    fun rank(
        base: List<WordCandidate>,
        personal: List<WordCandidate> = emptyList(),
        vocab: List<WordCandidate> = emptyList(),
        limit: Int = 3,
    ): List<Suggestion> {
        if (limit <= 0) return emptyList()

        // Best claim per word (case-insensitive). A word present in several sources keeps its
        // strongest source, so a learned/known word is never demoted to its base-dictionary self.
        val best = LinkedHashMap<String, Scored>()
        fun consider(candidate: WordCandidate, source: SuggestionSource) {
            val key = candidate.word.lowercase()
            val existing = best[key]
            if (existing == null ||
                source.priority > existing.source.priority ||
                (source.priority == existing.source.priority && candidate.frequency > existing.frequency)
            ) {
                best[key] = Scored(candidate.word, candidate.frequency, source)
            }
        }

        for (c in base) consider(c, SuggestionSource.BASE)
        for (c in personal) consider(c, SuggestionSource.PERSONAL)
        for (c in vocab) consider(c, SuggestionSource.VOCAB)

        return best.values
            .sortedWith(
                compareByDescending<Scored> { it.source.priority }.thenByDescending { it.frequency },
            )
            .take(limit)
            .map { Suggestion(it.word, it.source) }
    }

    private data class Scored(val word: String, val frequency: Int, val source: SuggestionSource)
}
