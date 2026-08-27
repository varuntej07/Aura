package dev.varuntej.aura.keyboard.prediction

import android.content.Context
import android.database.sqlite.SQLiteDatabase
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import java.io.File
import java.security.KeyStore
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class EncryptedPersonalizationStoreTest {
    private lateinit var context: Context
    private lateinit var store: EncryptedPersonalizationStore

    @Before
    fun setUp() {
        context = ApplicationProvider.getApplicationContext()
        store = EncryptedPersonalizationStore(context)
        store.clearAll()
    }

    @After
    fun tearDown() {
        store.clearAll()
    }

    @Test
    fun saveAndRestart_encryptsEveryPersonalizationCategory() {
        val state = populatedState(generation = 8)
        store.save(state)
        val encoded = encryptedFile().readBytes()
        assertFalse(encoded.toString(Charsets.ISO_8859_1).contains("AuraSecret"))
        val loaded = EncryptedPersonalizationStore(context).loadOrMigrate()
        assertEquals(PersonalizationStoreStatus.LOADED, loaded.status)
        assertEquals(8, loaded.state.generation)
        assertEquals(state.lexemes, loaded.state.lexemes)
        assertEquals(state.ngrams, loaded.state.ngrams)
        assertEquals(state.corrections, loaded.state.corrections)
        assertEquals(state.pending, loaded.state.pending)
        assertEquals(state.provenanceCounters, loaded.state.provenanceCounters)
    }

    @Test
    fun plaintextV1MigratesOnce_thenDeletesDbWalShmAndJournal() {
        val legacy = context.getDatabasePath(LEGACY_DB)
        legacy.parentFile?.mkdirs()
        SQLiteDatabase.openOrCreateDatabase(legacy, null).use { database ->
            database.execSQL(
                "CREATE TABLE personal_words (display TEXT PRIMARY KEY, count INTEGER, last_used INTEGER)",
            )
            database.execSQL(
                "INSERT INTO personal_words(display, count, last_used) VALUES ('AuraSecret', 4, 123)",
            )
        }
        File(legacy.path + "-wal").writeBytes(byteArrayOf(1))
        File(legacy.path + "-shm").writeBytes(byteArrayOf(1))
        File(legacy.path + "-journal").writeBytes(byteArrayOf(1))

        val migrated = store.loadOrMigrate()

        assertEquals(PersonalizationStoreStatus.MIGRATED, migrated.status)
        assertEquals(4, migrated.state.lexemes.getValue("aurasecret").legacyCount)
        assertTrue(encryptedFile().isFile)
        assertLegacyArtifactsAbsent()
    }

    @Test
    fun corruptCiphertextAndInvalidatedKey_failOpenAndReset() {
        store.save(populatedState(generation = 3))
        encryptedFile().writeBytes("not ciphertext".toByteArray())
        val corrupted = EncryptedPersonalizationStore(context).loadOrMigrate()
        assertEquals(PersonalizationStoreStatus.CORRUPT_RESET, corrupted.status)
        assertTrue(corrupted.state.lexemes.isEmpty())
        assertFalse(encryptedFile().exists())

        store.save(populatedState(generation = 4))
        KeyStore.getInstance(KEYSTORE).apply {
            load(null)
            deleteEntry(KEY_ALIAS)
        }
        val invalidated = EncryptedPersonalizationStore(context).loadOrMigrate()
        assertEquals(PersonalizationStoreStatus.CORRUPT_RESET, invalidated.status)
        assertTrue(invalidated.state.lexemes.isEmpty())
    }

    @Test
    fun clearDestroysKeyCiphertextLegacyArtifacts_andOldQueuedWritesCannotResurrect() {
        val dictionary = LocalPersonalizationDictionary(context)
        repeat(PersonalizationPolicy.MAX_EVENT_QUEUE) { index ->
            dictionary.record(PersonalizationEvent.ExplicitAdd("stale${letters(index)}", index.toLong()))
        }
        val cleared = CountDownLatch(1)
        var clearSucceeded = false
        dictionary.clearAll { success ->
            clearSucceeded = success
            cleared.countDown()
        }
        assertTrue(cleared.await(5, TimeUnit.SECONDS))
        assertTrue(clearSucceeded)
        dictionary.close()

        val reopened = EncryptedPersonalizationStore(context).loadOrMigrate()
        assertTrue(reopened.state.lexemes.isEmpty())
        assertFalse(encryptedFile().exists())
        assertLegacyArtifactsAbsent()
        val keyStore = KeyStore.getInstance(KEYSTORE).apply { load(null) }
        assertFalse(keyStore.containsAlias(KEY_ALIAS))
    }

    private fun populatedState(generation: Long): PersonalizationState {
        val state = PersonalizationState(generation = generation)
        state.lexemes["aurasecret"] = PersonalLexeme(
            "aurasecret", "AuraSecret", 2, 1, 0, 0, 100,
        )
        val ngram = NGramRecord(listOf("hello", "aura"), "buddy", 3, 100)
        state.ngrams[ngram.stableKey()] = ngram
        val correction = CorrectionEvidence("teh", "the", 3, 0, 100)
        state.corrections[correction.stableKey()] = correction
        state.pending[1] = PendingPositive(
            1, LearningProvenance.MANUAL_TYPED, "privateword", "PrivateWord",
            "hello", null, 2_000,
        )
        state.provenanceCounters[LearningProvenance.MANUAL_TYPED] = 9
        return state
    }

    private fun encryptedFile() =
        File(context.noBackupFilesDir, "buddy_keyboard_personalization.v2.enc")

    private fun assertLegacyArtifactsAbsent() {
        val legacy = context.getDatabasePath(LEGACY_DB)
        listOf(legacy, File(legacy.path + "-wal"), File(legacy.path + "-shm"),
            File(legacy.path + "-journal")).forEach { assertFalse(it.exists()) }
    }

    private fun letters(value: Int): String {
        var number = value
        return buildString {
            repeat(4) {
                append(('a'.code + number % 26).toChar())
                number /= 26
            }
        }
    }

    private companion object {
        const val LEGACY_DB = "buddy_personal_dictionary.db"
        const val KEYSTORE = "AndroidKeyStore"
        const val KEY_ALIAS = "aura_keyboard_personalization_v2"
    }
}
