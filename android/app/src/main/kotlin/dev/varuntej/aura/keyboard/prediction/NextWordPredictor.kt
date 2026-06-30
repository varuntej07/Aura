package dev.varuntej.aura.keyboard.prediction

import android.content.Context
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicBoolean

/**
 * Next-word prediction shown after the user commits a word + space, so the suggestion strip stays
 * useful instead of going blank. A first-order Markov model: if a bigram table has continuations
 * for the previous word, those are offered; otherwise it falls back to the most common English
 * words (the unigram prior).
 *
 * v1 ships the unigram fallback only (the `dictionaries/en_top100.txt` asset). A bigram table
 * (`en_bigrams.txt`) can drop in later as an asset with no code change: [rank] already prefers
 * bigrams when present. Asset load is lazy and off the UI thread (mirrors [BaseDictionary]); until
 * it finishes, [predictAfter] simply returns nothing. The [rank] core is pure and unit-tested.
 * 100% on-device.
 */
object NextWordPredictor {

    private const val UNIGRAM_ASSET = "dictionaries/en_top100.txt"

    @Volatile
    private var unigrams: List<String> = emptyList()

    // First word (lowercased) -> ranked next words. Empty until a bigram asset ships; reserved for
    // that drop-in. [rank] prefers it over the unigram prior whenever a key has entries.
    @Volatile
    private var bigrams: Map<String, List<String>> = emptyMap()

    private val loadStarted = AtomicBoolean(false)
    private val executor = Executors.newSingleThreadExecutor()

    val isLoaded: Boolean get() = unigrams.isNotEmpty()

    /** Start loading the unigram asset if it isn't loaded or loading already. Safe to call on
     *  every focus; the work runs once, off the UI thread. */
    fun ensureLoaded(context: Context) {
        if (unigrams.isNotEmpty() || !loadStarted.compareAndSet(false, true)) return
        val appContext = context.applicationContext
        executor.execute {
            try {
                unigrams = loadUnigrams(appContext)
            } catch (t: Throwable) {
                loadStarted.set(false) // allow a later focus to retry
            }
        }
    }

    /** Up to [limit] likely next words after [previousWord], using whatever is loaded. */
    fun predictAfter(previousWord: String, limit: Int): List<String> =
        rank(previousWord, bigrams, unigrams, limit)

    /**
     * Pure ranking: the bigram continuations for [previousWord] if any, else the [unigrams] prior,
     * always excluding [previousWord] itself so the strip never suggests the word just typed.
     */
    fun rank(
        previousWord: String,
        bigrams: Map<String, List<String>>,
        unigrams: List<String>,
        limit: Int,
    ): List<String> {
        if (limit <= 0) return emptyList()
        val byBigram = bigrams[previousWord.lowercase()].orEmpty()
        val source = if (byBigram.isNotEmpty()) byBigram else unigrams
        return source.asSequence()
            .filter { !it.equals(previousWord, ignoreCase = true) }
            .distinct()
            .take(limit)
            .toList()
    }

    private fun loadUnigrams(context: Context): List<String> {
        val out = ArrayList<String>(100)
        context.assets.open(UNIGRAM_ASSET).bufferedReader().use { reader ->
            reader.forEachLine { line ->
                val word = line.trim()
                if (word.isNotEmpty()) out.add(word)
            }
        }
        return out
    }
}
