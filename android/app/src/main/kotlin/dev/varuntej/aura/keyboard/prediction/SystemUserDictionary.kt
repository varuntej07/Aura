package dev.varuntej.aura.keyboard.prediction

import android.content.Context
import android.provider.UserDictionary
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicBoolean

/**
 * The words the user has added to Android's own system dictionary (Settings ->
 * "Personal dictionary"), exposed read-only through the `UserDictionary.Words` content provider.
 * Reading the user's own dictionary needs no permission. These words are merged into the
 * suggestion strip's personal tier and counted as "known" (so they are never flagged or
 * autocorrected in a later milestone).
 *
 * Cached in memory in a [PrefixIndex] and refreshed at most every [REFRESH_INTERVAL_MS] off the
 * UI thread, so a content-provider query never lands on the hot typing path. The user dictionary
 * is tiny, so this is cheap. Reads return whatever is currently cached (empty until the first
 * load finishes). 100% on-device.
 */
object SystemUserDictionary {

    private const val REFRESH_INTERVAL_MS = 10 * 60 * 1000L

    @Volatile
    private var index: PrefixIndex = PrefixIndex.from(emptyList())

    @Volatile
    private var lastLoadedAt = 0L
    private val loading = AtomicBoolean(false)
    private val executor = Executors.newSingleThreadExecutor()

    /** Refresh the cache if it is stale (or never loaded). Safe to call on every focus. */
    fun ensureFresh(context: Context) {
        if (System.currentTimeMillis() - lastLoadedAt < REFRESH_INTERVAL_MS) return
        if (!loading.compareAndSet(false, true)) return
        val appContext = context.applicationContext
        executor.execute {
            try {
                index = PrefixIndex.from(query(appContext))
                lastLoadedAt = System.currentTimeMillis()
            } catch (t: Throwable) {
                // Leave the previous cache in place; retry after the interval.
            } finally {
                loading.set(false)
            }
        }
    }

    fun completions(prefix: String, limit: Int): List<WordCandidate> =
        index.completions(prefix, limit)

    fun contains(word: String): Boolean = index.contains(word)

    private fun query(context: Context): List<WordCandidate> {
        val out = ArrayList<WordCandidate>()
        val columns = arrayOf(UserDictionary.Words.WORD, UserDictionary.Words.FREQUENCY)
        context.contentResolver.query(
            UserDictionary.Words.CONTENT_URI, columns, null, null, null,
        )?.use { cursor ->
            val wordCol = cursor.getColumnIndex(UserDictionary.Words.WORD)
            val freqCol = cursor.getColumnIndex(UserDictionary.Words.FREQUENCY)
            if (wordCol < 0) return out
            while (cursor.moveToNext()) {
                val word = cursor.getString(wordCol)
                if (word.isNullOrBlank()) continue
                val freq = if (freqCol >= 0) cursor.getInt(freqCol) else 0
                out.add(WordCandidate(word, freq))
            }
        }
        return out
    }
}
