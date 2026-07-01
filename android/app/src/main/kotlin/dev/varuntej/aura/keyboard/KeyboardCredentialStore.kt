package dev.varuntej.aura.keyboard

import android.content.Context
import android.content.SharedPreferences
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKey
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicBoolean

/**
 * The cross-process bridge between the signed-in Aura app and the Buddy Keyboard.
 *
 * The keyboard runs in its own OS process and cannot see the app's in-memory Firebase
 * session, so the app writes a credential here (on sign-in and on token refresh) and
 * the keyboard reads it when it needs to call POST /keyboard/draft. Storage is the
 * app's own EncryptedSharedPreferences, readable only within this app's UID.
 *
 * What is shared: the app's short-lived Firebase ID token, the uid, and the active API
 * base URL (so a --dart-define candidate override reaches the keyboard with no native
 * change). The backend already verifies the ID token (resolve_keyboard_uid ->
 * verify_id_token), so no backend change is needed for this interim.
 *
 * Multi-process caveat: SharedPreferences (including EncryptedSharedPreferences) is officially
 * single-process, so the keyboard process can briefly read a value the app process just wrote.
 * That is benign here by design: the keyboard never trusts the stored ID token (it always mints a
 * FRESH one via [KeyboardAuth]), and it re-warms the cached apiBaseUrl on every field focus, so a
 * candidate-URL switch converges within one focus. Only apiBaseUrl can lag, and only momentarily.
 *
 * INTERIM (M0.2 lite): the production design mints a dedicated, revocable keyboard
 * token at POST /keyboard/token. That swap lands behind this same read API without the
 * keyboard changing.
 */
object KeyboardCredentialStore {

    private const val FILE_NAME = "buddy_keyboard_credential"
    private const val KEY_ID_TOKEN = "id_token"
    private const val KEY_UID = "uid"
    private const val KEY_API_BASE_URL = "api_base_url"

    data class Credential(val idToken: String, val uid: String, val apiBaseUrl: String)

    // Decrypting EncryptedSharedPreferences hits the AndroidKeyStore + disk, which is far too slow
    // to do on the IME main thread on every draft/voice tap. The keyboard process warms this cache
    // off the main thread (via [warmCache] on field focus) and reads [cachedCredential] on the hot
    // path instead. The app process keeps it fresh on each save/clear. @Volatile: written on a
    // worker, read on the main thread.
    @Volatile
    private var cached: Credential? = null
    private val priming = AtomicBoolean(false)
    private val ioExecutor = Executors.newSingleThreadExecutor()

    // A fresh prefs instance per call on purpose: the writer lives in a different process
    // (the app), so we never cache a handle that could go stale against its writes.
    private fun prefs(context: Context): SharedPreferences {
        val masterKey = MasterKey.Builder(context)
            .setKeyScheme(MasterKey.KeyScheme.AES256_GCM)
            .build()
        return EncryptedSharedPreferences.create(
            context,
            FILE_NAME,
            masterKey,
            EncryptedSharedPreferences.PrefKeyEncryptionScheme.AES256_SIV,
            EncryptedSharedPreferences.PrefValueEncryptionScheme.AES256_GCM,
        )
    }

    /** Called from the app process (via MethodChannel) on sign-in and token refresh. */
    fun save(context: Context, uid: String, idToken: String, apiBaseUrl: String) {
        prefs(context).edit()
            .putString(KEY_UID, uid)
            .putString(KEY_ID_TOKEN, idToken)
            .putString(KEY_API_BASE_URL, apiBaseUrl)
            .apply()
        cached = Credential(idToken = idToken, uid = uid, apiBaseUrl = apiBaseUrl)
    }

    /** Called from the app process on sign-out / revoke. */
    fun clear(context: Context) {
        prefs(context).edit().clear().apply()
        cached = null
    }

    /**
     * The current credential, or null if the user has never signed in (or signed out).
     * Decrypts on the calling thread, so call it OFF the main thread (or use [prime] +
     * [cachedCredential]). Defensive: any decryption failure degrades to null so the keyboard
     * shows a graceful "sign in" state instead of crashing.
     */
    fun read(context: Context): Credential? {
        val result = try {
            val p = prefs(context)
            val token = p.getString(KEY_ID_TOKEN, null)
            val uid = p.getString(KEY_UID, null)
            val baseUrl = p.getString(KEY_API_BASE_URL, null)
            if (token.isNullOrBlank() || uid.isNullOrBlank() || baseUrl.isNullOrBlank()) {
                null
            } else {
                Credential(idToken = token, uid = uid, apiBaseUrl = baseUrl)
            }
        } catch (t: Throwable) {
            null
        }
        cached = result
        return result
    }

    /** The last value [read]/[warmCache] loaded, without touching the keystore (instant, main-thread
     *  safe). May be null or a focus stale until the first warm completes; hot-path callers fall
     *  back to a default base URL and always mint a fresh token separately, so staleness is benign. */
    fun cachedCredential(): Credential? = cached

    /** Load the credential into [cachedCredential] off the main thread so the next hot-path read is
     *  instant. Coalesces concurrent calls (rapid field focuses) into at most one in-flight decrypt. */
    fun warmCache(context: Context) {
        val appContext = context.applicationContext
        if (!priming.compareAndSet(false, true)) return
        ioExecutor.execute {
            try {
                read(appContext)
            } finally {
                priming.set(false)
            }
        }
    }
}
