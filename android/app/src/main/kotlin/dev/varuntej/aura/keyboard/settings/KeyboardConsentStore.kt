package dev.varuntej.aura.keyboard.settings

import android.content.Context

/**
 * Whether the user has agreed to a specific network feature.
 *
 * Tri-state on purpose: the keyboard has to tell "never asked" apart from "asked and said
 * no". Collapsing them into a boolean either re-prompts a user who already declined on every
 * single tap, or treats silence as agreement. Neither is consent.
 */
enum class KeyboardConsentState(val storedValue: String) {
    UNSET("unset"),
    GRANTED("granted"),
    DECLINED("declined");

    val isGranted: Boolean get() = this == GRANTED

    companion object {
        fun fromStoredValue(value: String?): KeyboardConsentState =
            entries.firstOrNull { it.storedValue == value } ?: UNSET
    }
}

data class KeyboardConsentSnapshot(
    val localIntroSeen: Boolean = false,
    val introPromptsShown: Int = 0,
    val aiText: KeyboardConsentState = KeyboardConsentState.UNSET,
    val voice: KeyboardConsentState = KeyboardConsentState.UNSET,
    val lastUid: String = "",
)

/**
 * Device-local record of what the user has explicitly agreed to send off this phone.
 *
 * Holds no typed text and no learned vocabulary: booleans, an enum per network feature, and
 * the uid the current personalization belongs to. Mirrors [KeyboardSettingsStore] deliberately
 * so there is one storage idiom in the keyboard rather than two.
 *
 * Ordinary typing never consults this. Only the two paths that transmit do:
 * POST /keyboard/draft and the LiveKit voice session.
 */
object KeyboardConsentStore {
    private const val PREFS_NAME = "aura_keyboard_consent"
    private const val LOCAL_INTRO_SEEN = "local_intro_seen"
    private const val INTRO_PROMPTS_SHOWN = "intro_prompts_shown"
    private const val AI_TEXT_CONSENT = "ai_text_consent"
    private const val VOICE_CONSENT = "voice_consent"
    private const val CONSENT_VERSION = "consent_version"
    private const val LAST_UID = "last_uid"

    /**
     * Bump when the disclosure copy changes what is actually sent. A stored GRANTED from an
     * older version reads back as UNSET, so the user agrees to the new wording rather than
     * being held to an agreement they never saw.
     */
    private const val CURRENT_CONSENT_VERSION = 1

    /** How many times the first-use line is offered before it stops asking for attention. */
    const val MAX_INTRO_PROMPTS = 3

    fun read(context: Context): KeyboardConsentSnapshot {
        val prefs = prefs(context)
        val stale = prefs.getInt(CONSENT_VERSION, 0) < CURRENT_CONSENT_VERSION
        return KeyboardConsentSnapshot(
            localIntroSeen = prefs.getBoolean(LOCAL_INTRO_SEEN, false),
            introPromptsShown = prefs.getInt(INTRO_PROMPTS_SHOWN, 0),
            aiText = consent(prefs.getString(AI_TEXT_CONSENT, null), stale),
            voice = consent(prefs.getString(VOICE_CONSENT, null), stale),
            lastUid = prefs.getString(LAST_UID, null).orEmpty(),
        )
    }

    fun aiTextGranted(context: Context): Boolean = read(context).aiText.isGranted

    fun voiceGranted(context: Context): Boolean = read(context).voice.isGranted

    fun setAiTextConsent(context: Context, granted: Boolean) =
        writeConsent(context, AI_TEXT_CONSENT, granted)

    fun setVoiceConsent(context: Context, granted: Boolean) =
        writeConsent(context, VOICE_CONSENT, granted)

    fun markLocalIntroSeen(context: Context) {
        prefs(context).edit()
            .putBoolean(LOCAL_INTRO_SEEN, true)
            .putInt(CONSENT_VERSION, CURRENT_CONSENT_VERSION)
            .apply()
    }

    fun recordIntroPromptShown(context: Context) {
        val prefs = prefs(context)
        prefs.edit()
            .putInt(INTRO_PROMPTS_SHOWN, prefs.getInt(INTRO_PROMPTS_SHOWN, 0) + 1)
            .apply()
    }

    /**
     * Forget every network agreement on this device. Called when the signed-in account
     * changes: the person now holding the phone has agreed to nothing, and inheriting the
     * previous user's "yes" would send their text on someone else's authority.
     */
    fun resetNetworkConsent(context: Context) {
        prefs(context).edit()
            .remove(AI_TEXT_CONSENT)
            .remove(VOICE_CONSENT)
            .apply()
    }

    fun lastUid(context: Context): String = read(context).lastUid

    fun setLastUid(context: Context, uid: String) {
        prefs(context).edit().putString(LAST_UID, uid).apply()
    }

    private fun consent(stored: String?, stale: Boolean): KeyboardConsentState {
        val state = KeyboardConsentState.fromStoredValue(stored)
        // A stale GRANTED is re-asked; a stale DECLINED is left alone, because re-prompting
        // someone who already said no is the behaviour this store exists to prevent.
        return if (stale && state == KeyboardConsentState.GRANTED) KeyboardConsentState.UNSET else state
    }

    private fun writeConsent(context: Context, key: String, granted: Boolean) {
        val state = if (granted) KeyboardConsentState.GRANTED else KeyboardConsentState.DECLINED
        prefs(context).edit()
            .putString(key, state.storedValue)
            .putInt(CONSENT_VERSION, CURRENT_CONSENT_VERSION)
            .apply()
    }

    private fun prefs(context: Context) =
        (context.applicationContext ?: context)
            .getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
}
