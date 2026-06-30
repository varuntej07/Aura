package dev.varuntej.aura.keyboard.prediction

import android.content.ContentValues
import android.content.Context
import android.database.sqlite.SQLiteDatabase
import android.database.sqlite.SQLiteOpenHelper
import android.os.Handler
import android.os.Looper
import java.util.concurrent.Executors

/**
 * The user's locally-learned words. The keyboard reads [completions]/[contains] on the hot
 * typing path and calls [learn] when a word is committed in a learning-allowed field. 100%
 * on-device; nothing here is ever uploaded.
 *
 * An interface so the storage choice stays swappable (a future Room/AOSP-binary-dict backend
 * would drop in behind it with no IME change).
 */
interface PersonalDictionary {
    fun completions(prefix: String, limit: Int): List<WordCandidate>
    fun contains(word: String): Boolean
    /** Record a use of a committed word (new -> count 1, existing -> count + 1). */
    fun learn(word: String)
    /** Explicitly pin a word as known (the long-press "add" action). */
    fun add(word: String)
    /** Forget a word (the long-press "remove" action). */
    fun remove(word: String)
    /** Release the backing storage (DB connection + I/O thread). Called when the IME is destroyed
     *  so a recreated keyboard does not accumulate a leaked thread + SQLite connection each time. */
    fun close()
}

/**
 * SQLite-backed [PersonalDictionary]: a single tiny table fronted by an in-memory
 * [PersonalDictionaryCache]. Reads always hit the cache (synchronous, allocation-light); writes
 * update the cache immediately and are mirrored to disk on a background thread, so disk never
 * blocks typing. The cache is the source of truth at runtime; on the next process start it is
 * rehydrated from the table.
 *
 * Deliberately NOT Room: there is no KSP/codegen in this build, and one table does not justify
 * introducing it. Hand-rolled SQLite via [SQLiteOpenHelper] is the framework-native equivalent.
 * The DB holds only ordinary dictionary words from non-secure fields (the IME never calls
 * [learn] from a secure/email/url field), so it is stored plainly, not encrypted.
 *
 * Threading: writes (learn / add / remove) come from the IME main thread, and the startup load
 * publishes via the main-thread Handler; reads (completions / contains) may also come from the
 * IME's background prediction thread. [PersonalDictionaryCache] uses a ConcurrentHashMap so those
 * concurrent reads are safe. All DB I/O runs on a single background executor.
 */
class SqlitePersonalDictionary(context: Context) : PersonalDictionary {

    private val helper = Helper(context.applicationContext)
    private val cache = PersonalDictionaryCache()
    private val mainHandler = Handler(Looper.getMainLooper())
    private val ioExecutor = Executors.newSingleThreadExecutor()

    init {
        // Rehydrate the cache from disk once, off the UI thread, then publish on the main thread.
        ioExecutor.execute {
            val loaded = try {
                readAll()
            } catch (t: Throwable) {
                emptyList()
            }
            mainHandler.post { cache.load(loaded) }
        }
    }

    override fun completions(prefix: String, limit: Int): List<WordCandidate> =
        cache.completions(prefix, limit, System.currentTimeMillis())

    override fun contains(word: String): Boolean = cache.contains(word)

    override fun learn(word: String) {
        persist(cache.learn(word, System.currentTimeMillis()))
    }

    override fun add(word: String) {
        persist(cache.add(word, System.currentTimeMillis()))
    }

    override fun remove(word: String) {
        cache.remove(word)
        val key = word.lowercase()
        ioExecutor.execute {
            try {
                helper.writableDatabase.delete(TABLE, "$COL_WORD = ?", arrayOf(key))
            } catch (_: Throwable) {
                // Persistence is best-effort; the cache already reflects the change this session.
            }
        }
    }

    private fun persist(entry: PersonalWord) {
        val key = entry.word.lowercase()
        ioExecutor.execute {
            try {
                val values = ContentValues().apply {
                    put(COL_WORD, key)
                    put(COL_DISPLAY, entry.word)
                    put(COL_COUNT, entry.count)
                    put(COL_LAST_USED, entry.lastUsed)
                }
                helper.writableDatabase.insertWithOnConflict(
                    TABLE, null, values, SQLiteDatabase.CONFLICT_REPLACE,
                )
            } catch (_: Throwable) {
            }
        }
    }

    override fun close() {
        // Drain on the I/O thread so any queued write finishes before the connection closes, then
        // shut the executor down. No DB work touches the main thread; the cache stays the runtime
        // source of truth and is rehydrated from disk on the next process start.
        ioExecutor.execute {
            try {
                helper.close()
            } catch (_: Throwable) {
            }
        }
        ioExecutor.shutdown()
    }

    private fun readAll(): List<PersonalWord> {
        val out = ArrayList<PersonalWord>()
        helper.readableDatabase.query(
            TABLE, arrayOf(COL_DISPLAY, COL_COUNT, COL_LAST_USED), null, null, null, null, null,
        ).use { cursor ->
            while (cursor.moveToNext()) {
                out.add(PersonalWord(cursor.getString(0), cursor.getInt(1), cursor.getLong(2)))
            }
        }
        return out
    }

    private class Helper(context: Context) :
        SQLiteOpenHelper(context, DB_NAME, null, DB_VERSION) {
        override fun onCreate(db: SQLiteDatabase) {
            db.execSQL(
                "CREATE TABLE $TABLE (" +
                    "$COL_WORD TEXT PRIMARY KEY, " +
                    "$COL_DISPLAY TEXT NOT NULL, " +
                    "$COL_COUNT INTEGER NOT NULL, " +
                    "$COL_LAST_USED INTEGER NOT NULL)",
            )
        }

        override fun onUpgrade(db: SQLiteDatabase, oldVersion: Int, newVersion: Int) {
            // v1 schema only; future migrations go here.
        }
    }

    companion object {
        private const val DB_NAME = "buddy_personal_dictionary.db"
        private const val DB_VERSION = 1
        private const val TABLE = "personal_words"
        private const val COL_WORD = "word"
        private const val COL_DISPLAY = "display"
        private const val COL_COUNT = "count"
        private const val COL_LAST_USED = "last_used"
    }
}
