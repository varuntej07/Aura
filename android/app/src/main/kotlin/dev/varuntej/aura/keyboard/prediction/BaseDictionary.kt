package dev.varuntej.aura.keyboard.prediction

import android.content.Context
import java.io.FileInputStream
import java.nio.channels.FileChannel
import java.util.concurrent.atomic.AtomicBoolean

/**
 * The bundled base English dictionary, loaded once per keyboard process from the deterministic
 * `dictionaries/en_us.pdict` packed radix asset.
 *
 * Loading is lazy and off the UI thread (the asset is ~30k lines): [ensureLoaded] kicks the
 * load on the first prediction-allowed focus and returns immediately; until it finishes,
 * [completions]/[contains]/[frequencyOf] simply return empty/false/0, so the suggestion strip
 * is briefly blank rather than ever blocking typing. The index is a process-wide singleton, so
 * the cost is paid once. 100% on-device; nothing here ever touches the network.
 *
 * The APK stores the asset uncompressed, so Android can expose a file descriptor and the IME can
 * memory-map it. If mapping or validation fails, every query returns empty and ordinary typing
 * continues; loading or recovering the dictionary is never a prerequisite for [commitText].
 */
object BaseDictionary {

    private const val ASSET_PATH = "dictionaries/en_us.pdict"

    @Volatile
    private var dictionary: PackedDictionary? = null
    private val loadStarted = AtomicBoolean(false)

    @Volatile
    var runtimeInfo: RuntimeInfo? = null
        private set

    val isLoaded: Boolean get() = dictionary != null

    /** Start loading the dictionary if it isn't loaded or loading already. Safe to call on
     *  every focus; the work runs once, off the UI thread. */
    fun ensureLoaded(context: Context) {
        if (dictionary != null || !loadStarted.compareAndSet(false, true)) return
        val appContext = context.applicationContext
        Thread({
            dictionary = try {
                val startedAt = System.nanoTime()
                val (loaded, bytes) = load(appContext)
                runtimeInfo = RuntimeInfo(
                    packagedBytes = bytes,
                    nodeCount = loaded.nodeCount,
                    edgeCount = loaded.edgeCount,
                    wordCount = loaded.wordCount,
                    mappedLoadMillis = (System.nanoTime() - startedAt) / 1_000_000.0,
                )
                loaded
            } catch (t: Throwable) {
                // Allow a later focus to retry rather than wedging "never loaded".
                loadStarted.set(false)
                runtimeInfo = null
                null
            }
        }, "AuraImeBaseDictionaryLoad").apply {
            isDaemon = true
            start()
        }
    }

    fun completions(prefix: String, limit: Int): List<WordCandidate> =
        dictionary?.completions(prefix, limit) ?: emptyList()

    fun contains(word: String): Boolean = dictionary?.contains(word) ?: false

    fun frequencyOf(word: String): Int = dictionary?.frequencyOf(word) ?: 0

    fun corrections(
        word: String,
        limit: Int,
        maxEditDistance: Int,
        cancellation: PredictionCancellation,
    ): List<CorrectionCandidate> =
        dictionary?.corrections(word, limit, maxEditDistance, cancellation) ?: emptyList()

    private fun load(context: Context): Pair<PackedDictionary, Long> {
        context.assets.openFd(ASSET_PATH).use { descriptor ->
            FileInputStream(descriptor.fileDescriptor).channel.use { channel ->
                val mapped = channel.map(
                    FileChannel.MapMode.READ_ONLY,
                    descriptor.startOffset,
                    descriptor.length,
                )
                return PackedDictionary.from(mapped) to descriptor.length
            }
        }
    }

    data class RuntimeInfo(
        val packagedBytes: Long,
        val nodeCount: Int,
        val edgeCount: Int,
        val wordCount: Int,
        val mappedLoadMillis: Double,
    )
}
