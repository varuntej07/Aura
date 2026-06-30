package dev.varuntej.aura.keyboard.prediction

import android.content.Context
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicBoolean

/**
 * The bundled base English dictionary (the `dictionaries/en_wordlist.txt` asset), loaded once
 * per keyboard process into an in-memory [PrefixIndex].
 *
 * Loading is lazy and off the UI thread (the asset is ~30k lines): [ensureLoaded] kicks the
 * load on the first prediction-allowed focus and returns immediately; until it finishes,
 * [completions]/[contains]/[frequencyOf] simply return empty/false/0, so the suggestion strip
 * is briefly blank rather than ever blocking typing. The index is a process-wide singleton, so
 * the cost is paid once. 100% on-device; nothing here ever touches the network.
 *
 * Thin by design (asset IO only); all lookup logic lives in the pure, unit-tested [PrefixIndex].
 */
object BaseDictionary {

    private const val ASSET_PATH = "dictionaries/en_wordlist.txt"

    @Volatile
    private var index: PrefixIndex? = null
    private val loadStarted = AtomicBoolean(false)
    private val executor = Executors.newSingleThreadExecutor()

    val isLoaded: Boolean get() = index != null

    /** Start loading the dictionary if it isn't loaded or loading already. Safe to call on
     *  every focus; the work runs once, off the UI thread. */
    fun ensureLoaded(context: Context) {
        if (index != null || !loadStarted.compareAndSet(false, true)) return
        val appContext = context.applicationContext
        executor.execute {
            index = try {
                load(appContext)
            } catch (t: Throwable) {
                // Allow a later focus to retry rather than wedging "never loaded".
                loadStarted.set(false)
                null
            }
        }
    }

    fun completions(prefix: String, limit: Int): List<WordCandidate> =
        index?.completions(prefix, limit) ?: emptyList()

    fun contains(word: String): Boolean = index?.contains(word) ?: false

    fun frequencyOf(word: String): Int = index?.frequencyOf(word) ?: 0

    private fun load(context: Context): PrefixIndex {
        val entries = ArrayList<WordCandidate>(32_000)
        context.assets.open(ASSET_PATH).bufferedReader().use { reader ->
            reader.forEachLine { line ->
                // Each line is "word freq" (ASCII, space-separated).
                val space = line.indexOf(' ')
                if (space <= 0) return@forEachLine
                val word = line.substring(0, space)
                val freq = line.substring(space + 1).trim().toIntOrNull() ?: return@forEachLine
                entries.add(WordCandidate(word, freq))
            }
        }
        return PrefixIndex.from(entries)
    }
}
