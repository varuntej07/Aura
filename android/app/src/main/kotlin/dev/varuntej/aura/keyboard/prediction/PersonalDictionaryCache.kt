package dev.varuntej.aura.keyboard.prediction

import java.util.concurrent.ConcurrentHashMap
import kotlin.math.exp

/** A learned word with its usage count and last-used time (epoch millis). The persistence row. */
data class PersonalWord(val word: String, val count: Int, val lastUsed: Long)

/**
 * The in-memory model of the user's learned words: the hot-path source of truth that the
 * suggestion strip reads on every keystroke, so prediction never waits on disk. Pure and
 * unit-tested; [SqlitePersonalDictionary] wraps it with persistence and is the only thing that
 * touches storage.
 *
 * Keyed by the lowercased word (so matching is case-insensitive) but each entry keeps a display
 * form (the casing the user typed, so a learned proper noun like "Thiru" is offered capitalized).
 * Ranking within the personal tier is by a time-decayed score, then recency: a word's weight is
 * `count · e^(-elapsedDays / DECAY_TIME_CONSTANT_DAYS)`, so an old word the user no longer types
 * (an accidentally-learned typo) fades on its own without an explicit unlearn, while a recent word
 * outranks a stale higher-count one. The clock is injected (`atMillis` / `nowMs`) rather than read
 * here, keeping this class deterministic and Android-free.
 *
 * Threading: writes ([learn] / [add] / [remove]) come from the IME main thread; reads
 * ([completions] / [contains]) can come from the IME's background prediction thread. The backing
 * map is a [ConcurrentHashMap] so a concurrent read during a write is safe (a per-entry int read
 * may be a keystroke stale, which only shifts ranking momentarily, never crashes).
 */
class PersonalDictionaryCache {

    private class Entry(var display: String, var count: Int, var lastUsed: Long)

    private val byKey = ConcurrentHashMap<String, Entry>()

    val size: Int get() = byKey.size

    /** Record a use of [word] (new word -> count 1; existing -> count + 1). Returns the result. */
    fun learn(word: String, atMillis: Long): PersonalWord = bump(word, atMillis, increment = true)

    /** Ensure [word] is known without counting it as a fresh use (the explicit "pin" action). */
    fun add(word: String, atMillis: Long): PersonalWord = bump(word, atMillis, increment = false)

    private fun bump(word: String, atMillis: Long, increment: Boolean): PersonalWord {
        val key = word.lowercase()
        val entry = byKey[key]
        val result = if (entry == null) {
            Entry(word, 1, atMillis).also { byKey[key] = it }
        } else {
            if (increment) entry.count += 1
            entry.lastUsed = atMillis
            entry.display = word // track the latest casing the user typed
            entry
        }
        return PersonalWord(result.display, result.count, result.lastUsed)
    }

    /** Forget [word]. Returns true if it was present. */
    fun remove(word: String): Boolean = byKey.remove(word.lowercase()) != null

    fun contains(word: String): Boolean = byKey.containsKey(word.lowercase())

    /** A matching entry's ranking inputs captured as immutable values, so the sort below never
     *  reads a field that the main thread may mutate mid-comparison. */
    private class ScoredEntry(val display: String, val score: Double, val lastUsed: Long)

    /** Up to [limit] learned words starting with [prefix], strongest (highest time-decayed score
     *  at [nowMs], then most-recent) first. The personal dictionary is small (the user's own
     *  vocabulary), so a full scan per keystroke is cheap. The emitted [WordCandidate.frequency]
     *  carries the scaled decayed score, so the suggestion ranker orders this tier by decay too.
     *
     *  This runs on the background prediction thread while [learn]/[add] mutate the same entries on
     *  the main thread. Sorting directly over the live mutable [Entry] fields lets a concurrent
     *  write change a key mid-sort, which makes the comparator inconsistent and crashes TimSort
     *  ("Comparison method violates its general contract"). So each matching entry's score and
     *  recency are snapshotted into an immutable [ScoredEntry] BEFORE sorting; the sort then only
     *  ever sees stable values (a snapshot may be a keystroke stale, which only shifts ranking
     *  momentarily, exactly as the class's threading note already allows). */
    fun completions(prefix: String, limit: Int, nowMs: Long): List<WordCandidate> {
        if (prefix.isEmpty() || limit <= 0) return emptyList()
        val p = prefix.lowercase()
        return byKey.entries
            .filter { it.key.startsWith(p) }
            .map { ScoredEntry(it.value.display, decayedScore(it.value, nowMs), it.value.lastUsed) }
            .sortedWith(
                compareByDescending<ScoredEntry> { it.score }.thenByDescending { it.lastUsed },
            )
            .take(limit)
            .map { WordCandidate(it.display, (it.score * SCORE_SCALE).toInt()) }
    }

    /** A word's weight at [nowMs]: usage count attenuated by exponential time decay, so weight
     *  falls to ~37% (1/e) after [DECAY_TIME_CONSTANT_DAYS]. A future `lastUsed` (clock skew) is
     *  clamped to zero elapsed so it cannot inflate the score. */
    private fun decayedScore(entry: Entry, nowMs: Long): Double {
        val elapsedDays = (nowMs - entry.lastUsed).coerceAtLeast(0L).toDouble() / MILLIS_PER_DAY
        return entry.count * exp(-elapsedDays / DECAY_TIME_CONSTANT_DAYS)
    }

    /** All entries, for persisting. */
    fun snapshot(): List<PersonalWord> =
        byKey.values.map { PersonalWord(it.display, it.count, it.lastUsed) }

    /** Replace the whole cache (used to publish the persisted words at startup). */
    fun load(words: List<PersonalWord>) {
        byKey.clear()
        for (w in words) byKey[w.word.lowercase()] = Entry(w.word, w.count, w.lastUsed)
    }

    private companion object {
        const val MILLIS_PER_DAY = 86_400_000.0
        // The decay time constant (tau), not a half-life: weight is e^(-t/tau), so it reaches
        // ~37% after this many days and ~14% after twice as many.
        const val DECAY_TIME_CONSTANT_DAYS = 90.0
        // Scales the (0..count] decayed score into the integer WordCandidate.frequency the ranker
        // compares within the personal tier.
        const val SCORE_SCALE = 1000.0
    }
}
