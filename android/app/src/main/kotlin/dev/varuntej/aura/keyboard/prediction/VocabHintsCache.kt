package dev.varuntej.aura.keyboard.prediction

import android.content.Context
import dev.varuntej.aura.keyboard.KeyboardAuth
import dev.varuntej.aura.keyboard.KeyboardCredentialStore
import org.json.JSONArray
import android.os.Handler
import android.os.Looper
import java.io.BufferedReader
import java.net.HttpURLConnection
import java.net.URL
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicBoolean

/**
 * The user's consent-gated vocab hints (interest subjects + storyline entities) downloaded from
 * GET /keyboard/vocab and cached on device. These tokens are treated as KNOWN words (so a
 * friend's or interest's name is never flagged or autocorrected) and boosted in the suggestion
 * strip (the vocab tier).
 *
 * READ-ONLY: this only downloads a small hint set; nothing the user types is ever uploaded. The
 * cache is refreshed at most once a day, off the UI thread, and persists across keyboard process
 * restarts in app-private storage. Reads return whatever is currently cached (empty until the
 * first load). Mirrors the lazy, in-memory [PrefixIndex] pattern of [BaseDictionary].
 */
object VocabHintsCache {

    private const val PREFS = "buddy_keyboard_vocab"
    private const val KEY_TOKENS = "tokens"
    private const val KEY_FETCHED_AT = "fetched_at"
    private const val REFRESH_INTERVAL_MS = 24L * 60 * 60 * 1000
    private const val CONNECT_TIMEOUT_MS = 8_000
    private const val READ_TIMEOUT_MS = 8_000

    // Prod fallback when the app hasn't bridged a base URL yet (matches BuddyImeService's default).
    private const val DEFAULT_API_BASE_URL = "https://juno-backend-620715294422.us-central1.run.app"

    @Volatile
    private var index: PrefixIndex = PrefixIndex.from(emptyList())

    // Proper-noun casing for the same tokens, so a name typed lowercase ("kcr") is committed in its
    // real form ("KCR"). Rebuilt alongside [index] in both the disk and network paths.
    @Volatile
    private var properNouns: ProperNounIndex = ProperNounIndex.EMPTY

    @Volatile
    private var lastFetchedAt = 0L
    private var loadedFromDisk = false
    private val busy = AtomicBoolean(false)
    private val executor = Executors.newSingleThreadExecutor()
    private val mainHandler = Handler(Looper.getMainLooper())

    fun completions(prefix: String, limit: Int): List<WordCandidate> =
        index.completions(prefix, limit)

    fun contains(word: String): Boolean = index.contains(word)

    /** The proper-noun display form of [word] (e.g. "kcr" -> "KCR") when it is a known vocab token
     *  typed in another casing; null otherwise. */
    fun properNounDisplayForm(word: String): String? = properNouns.displayForm(word)

    /**
     * Load the cached hints once, then refresh from the network if a day has passed. Safe to call
     * on every focus; a single in-flight cycle is guarded so focuses don't pile up requests.
     */
    fun ensureFresh(context: Context) {
        val appContext = context.applicationContext
        if (!busy.compareAndSet(false, true)) return
        executor.execute {
            if (!loadedFromDisk) {
                loadFromDisk(appContext)
                loadedFromDisk = true
            }
            val stale = System.currentTimeMillis() - lastFetchedAt > REFRESH_INTERVAL_MS
            if (!stale) {
                busy.set(false)
                return@execute
            }
            // A fresh ID token must be minted on the main thread (Firebase's callback executor),
            // then the GET runs back on the executor. busy stays held across the whole hop.
            mainHandler.post {
                KeyboardAuth.freshIdToken { token ->
                    if (token.isNullOrBlank()) {
                        busy.set(false)
                        return@freshIdToken
                    }
                    executor.execute {
                        try {
                            fetchAndStore(appContext, token)
                        } catch (_: Throwable) {
                            // Keep the existing cache; try again next interval.
                        } finally {
                            busy.set(false)
                        }
                    }
                }
            }
        }
    }

    private fun fetchAndStore(context: Context, idToken: String) {
        val baseUrl = KeyboardCredentialStore.read(context)?.apiBaseUrl ?: DEFAULT_API_BASE_URL
        val tokens = httpGetTokens(baseUrl, idToken) ?: return
        saveToDisk(context, tokens)
        index = PrefixIndex.from(tokens.map { WordCandidate(it, 1) })
        properNouns = ProperNounIndex.from(tokens)
        lastFetchedAt = System.currentTimeMillis()
    }

    private fun httpGetTokens(baseUrl: String, idToken: String): List<String>? {
        val endpoint = baseUrl.trimEnd('/') + "/keyboard/vocab"
        val conn = (URL(endpoint).openConnection() as HttpURLConnection).apply {
            requestMethod = "GET"
            connectTimeout = CONNECT_TIMEOUT_MS
            readTimeout = READ_TIMEOUT_MS
            setRequestProperty("Authorization", "Bearer $idToken")
        }
        try {
            if (conn.responseCode != HttpURLConnection.HTTP_OK) return null
            val text = conn.inputStream.bufferedReader().use(BufferedReader::readText)
            val arr = org.json.JSONObject(text).optJSONArray(KEY_TOKENS) ?: JSONArray()
            return (0 until arr.length()).mapNotNull { arr.optString(it).takeIf { s -> s.isNotBlank() } }
        } finally {
            conn.disconnect()
        }
    }

    private fun loadFromDisk(context: Context) {
        val prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
        lastFetchedAt = prefs.getLong(KEY_FETCHED_AT, 0L)
        val json = prefs.getString(KEY_TOKENS, null) ?: return
        try {
            val arr = JSONArray(json)
            val tokens = (0 until arr.length()).mapNotNull {
                arr.optString(it).takeIf { s -> s.isNotBlank() }
            }
            index = PrefixIndex.from(tokens.map { WordCandidate(it, 1) })
            properNouns = ProperNounIndex.from(tokens)
        } catch (_: Throwable) {
            // Corrupt cache: ignore; the next refresh repopulates it.
        }
    }

    private fun saveToDisk(context: Context, tokens: List<String>) {
        val prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
        prefs.edit()
            .putString(KEY_TOKENS, JSONArray(tokens).toString())
            .putLong(KEY_FETCHED_AT, System.currentTimeMillis())
            .apply()
    }
}
