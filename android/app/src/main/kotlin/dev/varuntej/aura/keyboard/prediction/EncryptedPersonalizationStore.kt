package dev.varuntej.aura.keyboard.prediction

import android.content.Context
import android.database.sqlite.SQLiteDatabase
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.AtomicFile
import java.io.ByteArrayInputStream
import java.io.ByteArrayOutputStream
import java.io.DataInputStream
import java.io.DataOutputStream
import java.io.File
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

internal enum class PersonalizationStoreStatus {
    EMPTY,
    LOADED,
    MIGRATED,
    CORRUPT_RESET,
    MEMORY_ONLY,
}

internal data class PersonalizationLoadResult(
    val state: PersonalizationState,
    val status: PersonalizationStoreStatus,
)

/** Versioned AES-GCM snapshot store with its key protected by Android Keystore. */
internal class EncryptedPersonalizationStore(context: Context) {
    private val appContext = context.applicationContext
    private val baseFile = File(appContext.noBackupFilesDir, ENCRYPTED_FILE)
    private val atomicFile = AtomicFile(baseFile)
    private val legacyFile = appContext.getDatabasePath(LEGACY_DB)

    fun loadOrMigrate(): PersonalizationLoadResult {
        if (baseFile.exists()) {
            return try {
                PersonalizationLoadResult(readEncrypted(), PersonalizationStoreStatus.LOADED)
            } catch (_: Throwable) {
                destroyEncryptedArtifacts()
                PersonalizationLoadResult(PersonalizationState(), PersonalizationStoreStatus.CORRUPT_RESET)
            }
        }
        if (!legacyFile.exists()) {
            return PersonalizationLoadResult(PersonalizationState(), PersonalizationStoreStatus.EMPTY)
        }
        return try {
            val migrated = readLegacy()
            migrated.generation = 1
            save(migrated)
            val validated = readEncrypted()
            require(validated.lexemes.size == migrated.lexemes.size)
            deleteLegacyArtifacts()
            PersonalizationLoadResult(validated, PersonalizationStoreStatus.MIGRATED)
        } catch (_: Throwable) {
            // Leave v1 untouched for a later foreground-process retry, but never resume plaintext
            // writes. The caller continues with an empty in-memory model.
            destroyEncryptedArtifacts()
            PersonalizationLoadResult(PersonalizationState(), PersonalizationStoreStatus.MEMORY_ONLY)
        }
    }

    fun save(state: PersonalizationState) {
        val payload = PersonalizationCodec.encode(state)
        val encrypted = encrypt(payload, state.generation)
        var stream = atomicFile.startWrite()
        try {
            stream.write(encrypted)
            atomicFile.finishWrite(stream)
        } catch (failure: Throwable) {
            atomicFile.failWrite(stream)
            throw failure
        }
    }

    fun clearAll() {
        destroyEncryptedArtifacts()
        deleteLegacyArtifacts()
        val keyStore = KeyStore.getInstance(KEYSTORE).apply { load(null) }
        check(!keyStore.containsAlias(KEY_ALIAS)) { "personalization key deletion failed" }
        check(encryptedArtifacts().none(File::exists)) { "encrypted personalization deletion failed" }
        check(legacyArtifacts().none(File::exists)) { "legacy personalization deletion failed" }
    }

    fun encryptedSizeBytes(): Long = baseFile.takeIf(File::isFile)?.length() ?: 0

    private fun readEncrypted(): PersonalizationState {
        val encoded = atomicFile.openRead().use { it.readBytes() }
        DataInputStream(ByteArrayInputStream(encoded)).use { input ->
            val magic = ByteArray(FILE_MAGIC.size).also(input::readFully)
            require(magic.contentEquals(FILE_MAGIC))
            require(input.readInt() == FILE_VERSION)
            val generation = input.readLong().also { require(it >= 0) }
            val ivLength = input.readInt().also { require(it in 12..16) }
            val ciphertextLength = input.readInt().also {
                require(it in GCM_TAG_BYTES..MAX_ENCRYPTED_BYTES)
            }
            require(input.available() == ivLength + ciphertextLength)
            val iv = ByteArray(ivLength).also(input::readFully)
            val ciphertext = ByteArray(ciphertextLength).also(input::readFully)
            val cipher = Cipher.getInstance(CIPHER)
            cipher.init(Cipher.DECRYPT_MODE, loadExistingKey(), GCMParameterSpec(GCM_TAG_BITS, iv))
            cipher.updateAAD(aad(generation))
            val state = PersonalizationCodec.decode(cipher.doFinal(ciphertext))
            require(state.generation == generation)
            return state
        }
    }

    private fun encrypt(payload: ByteArray, generation: Long): ByteArray {
        require(payload.size <= MAX_PLAINTEXT_BYTES)
        val cipher = Cipher.getInstance(CIPHER)
        cipher.init(Cipher.ENCRYPT_MODE, loadOrCreateKey())
        cipher.updateAAD(aad(generation))
        val ciphertext = cipher.doFinal(payload)
        return ByteArrayOutputStream().also { bytes ->
            DataOutputStream(bytes).use { out ->
                out.write(FILE_MAGIC)
                out.writeInt(FILE_VERSION)
                out.writeLong(generation)
                out.writeInt(cipher.iv.size)
                out.writeInt(ciphertext.size)
                out.write(cipher.iv)
                out.write(ciphertext)
            }
        }.toByteArray()
    }

    private fun loadExistingKey(): SecretKey {
        val keyStore = KeyStore.getInstance(KEYSTORE).apply { load(null) }
        return keyStore.getKey(KEY_ALIAS, null) as? SecretKey ?: error("personalization key missing")
    }

    private fun loadOrCreateKey(): SecretKey {
        val keyStore = KeyStore.getInstance(KEYSTORE).apply { load(null) }
        (keyStore.getKey(KEY_ALIAS, null) as? SecretKey)?.let { return it }
        return KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, KEYSTORE).run {
            init(
                KeyGenParameterSpec.Builder(
                    KEY_ALIAS,
                    KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT,
                ).setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                    .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                    .setRandomizedEncryptionRequired(true)
                    .setKeySize(256)
                    .build(),
            )
            generateKey()
        }
    }

    private fun readLegacy(): PersonalizationState {
        val state = PersonalizationState()
        SQLiteDatabase.openDatabase(legacyFile.path, null, SQLiteDatabase.OPEN_READONLY).use { db ->
            db.query(
                LEGACY_TABLE,
                arrayOf(LEGACY_DISPLAY, LEGACY_COUNT, LEGACY_LAST_USED),
                null,
                null,
                null,
                null,
                null,
            ).use { cursor ->
                while (cursor.moveToNext() && state.lexemes.size < PersonalizationPolicy.MAX_LEXEMES) {
                    val display = cursor.getString(0) ?: continue
                    val key = PersonalizationPolicy.normalizeLearnableToken(display) ?: continue
                    val count = cursor.getInt(1).coerceIn(1, MAX_LEGACY_COUNT)
                    val lastUsed = cursor.getLong(2).coerceAtLeast(0)
                    state.lexemes[key] = PersonalLexeme(
                        key = key,
                        display = display,
                        manualCount = 0,
                        acceptedCount = 0,
                        explicitCount = 0,
                        legacyCount = count,
                        lastUsedMillis = lastUsed,
                    )
                    state.provenanceCounters[LearningProvenance.LEGACY_IMPORT] =
                        state.provenanceCounters.getValue(LearningProvenance.LEGACY_IMPORT) + 1
                }
            }
        }
        state.enforceBounds(System.currentTimeMillis())
        return state
    }

    private fun destroyEncryptedArtifacts() {
        try {
            KeyStore.getInstance(KEYSTORE).apply {
                load(null)
                if (containsAlias(KEY_ALIAS)) deleteEntry(KEY_ALIAS)
            }
        } catch (_: Throwable) {
        }
        encryptedArtifacts().forEach {
            try {
                if (it.exists()) it.delete()
            } catch (_: Throwable) {
            }
        }
    }

    private fun deleteLegacyArtifacts() {
        legacyArtifacts().forEach {
            try {
                if (it.exists()) it.delete()
            } catch (_: Throwable) {
            }
        }
    }

    private fun encryptedArtifacts(): List<File> =
        listOf(baseFile, File(baseFile.path + ".new"), File(baseFile.path + ".bak"))

    private fun legacyArtifacts(): List<File> = listOf(
        legacyFile,
        File(legacyFile.path + "-wal"),
        File(legacyFile.path + "-shm"),
        File(legacyFile.path + "-journal"),
    )

    private fun aad(generation: Long): ByteArray = ByteArrayOutputStream().also { bytes ->
        DataOutputStream(bytes).use { out ->
            out.write(FILE_MAGIC)
            out.writeInt(FILE_VERSION)
            out.writeLong(generation)
        }
    }.toByteArray()

    private companion object {
        val FILE_MAGIC = "AURAE02!".toByteArray(Charsets.US_ASCII)
        const val FILE_VERSION = 2
        const val ENCRYPTED_FILE = "buddy_keyboard_personalization.v2.enc"
        const val KEY_ALIAS = "aura_keyboard_personalization_v2"
        const val KEYSTORE = "AndroidKeyStore"
        const val CIPHER = "AES/GCM/NoPadding"
        const val GCM_TAG_BITS = 128
        const val GCM_TAG_BYTES = GCM_TAG_BITS / 8
        const val MAX_PLAINTEXT_BYTES = 8 * 1024 * 1024
        const val MAX_ENCRYPTED_BYTES = MAX_PLAINTEXT_BYTES + 1024
        const val MAX_LEGACY_COUNT = 1_000_000
        const val LEGACY_DB = "buddy_personal_dictionary.db"
        const val LEGACY_TABLE = "personal_words"
        const val LEGACY_DISPLAY = "display"
        const val LEGACY_COUNT = "count"
        const val LEGACY_LAST_USED = "last_used"
    }
}
