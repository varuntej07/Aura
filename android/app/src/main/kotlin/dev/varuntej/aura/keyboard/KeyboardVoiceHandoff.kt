package dev.varuntej.aura.keyboard

import android.content.Context
import android.content.SharedPreferences
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKey

/**
 * One-shot handoff of the on-screen / field text from the Buddy Keyboard to the app's
 * voice session. When the user taps Voice in the keyboard, the keyboard stashes what is
 * in the field here and opens aura://voice; the app reads it once (and clears it) and
 * sends it to the tuned voice agent as screen context, so Buddy can talk about what is
 * on screen and reply. Same-UID EncryptedSharedPreferences, like KeyboardCredentialStore.
 *
 * The payload holds message content, so it is consumed once and cleared, and a read
 * ignores anything older than [STALE_MS] (a leftover from a tap that never opened voice).
 *
 * Multi-process caveat: SharedPreferences is officially single-process, but the write here is
 * ordered before the aura://voice activity launch that triggers the read, so the app process sees
 * the value; the [STALE_MS] window and read-once clear bound any edge case to "no context", never
 * stale context from a previous handoff.
 */
object KeyboardVoiceHandoff {

    private const val FILE_NAME = "buddy_keyboard_voice_handoff"
    private const val KEY_TEXT = "text"
    private const val KEY_FIELD_TYPE = "field_type"
    private const val KEY_APP = "app"
    private const val KEY_TS = "ts"
    private const val STALE_MS = 120_000L

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

    /** Keyboard process: stash the field text just before opening aura://voice. */
    fun write(context: Context, text: String, fieldType: String?, app: String?) {
        try {
            prefs(context).edit()
                .putString(KEY_TEXT, text)
                .putString(KEY_FIELD_TYPE, fieldType)
                .putString(KEY_APP, app)
                .putLong(KEY_TS, System.currentTimeMillis())
                .apply()
        } catch (t: Throwable) {
            // Best-effort: if the stash fails, voice still opens, just without context.
        }
    }

    /** App process: read once and clear. Returns null if absent, blank, or stale. */
    fun consume(context: Context): Map<String, Any?>? = try {
        val p = prefs(context)
        val text = p.getString(KEY_TEXT, null)
        val fieldType = p.getString(KEY_FIELD_TYPE, null)
        val app = p.getString(KEY_APP, null)
        val ts = p.getLong(KEY_TS, 0L)
        p.edit().clear().apply()
        if (text.isNullOrBlank() || System.currentTimeMillis() - ts > STALE_MS) {
            null
        } else {
            mapOf("text" to text, "field_type" to fieldType, "app" to app)
        }
    } catch (t: Throwable) {
        null
    }
}
