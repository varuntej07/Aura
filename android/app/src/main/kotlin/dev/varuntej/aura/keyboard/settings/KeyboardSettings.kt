package dev.varuntej.aura.keyboard.settings

import android.content.Context
import android.content.res.Configuration

enum class KeyboardThemeMode(val storedValue: String) {
    SYSTEM("system"),
    LIGHT("light"),
    DARK("dark");

    companion object {
        fun fromStoredValue(value: String?): KeyboardThemeMode =
            entries.firstOrNull { it.storedValue == value } ?: SYSTEM
    }
}

data class KeyboardSettingsSnapshot(
    val suggestions: Boolean = true,
    val autocorrect: Boolean = true,
    val learnNewWords: Boolean = true,
    val personalizedSuggestions: Boolean = true,
    val hapticFeedback: Boolean = false,
    val keypressSound: Boolean = false,
    val keyPreview: Boolean = false,
    val themeMode: KeyboardThemeMode = KeyboardThemeMode.SYSTEM,
    val advancedDiagnostics: Boolean = false,
    val revision: Long = 0L,
)

/** Plain, device-local behavior preferences. No typed text or learned vocabulary is stored here. */
object KeyboardSettingsStore {
    private const val PREFS_NAME = "aura_keyboard_settings"
    private const val SUGGESTIONS = "suggestions"
    private const val AUTOCORRECT = "autocorrect"
    private const val LEARN_NEW_WORDS = "learn_new_words"
    private const val PERSONALIZED_SUGGESTIONS = "personalized_suggestions"
    private const val HAPTIC_FEEDBACK = "haptic_feedback"
    private const val KEYPRESS_SOUND = "keypress_sound"
    private const val KEY_PREVIEW = "key_preview"
    private const val THEME_MODE = "theme_mode"
    private const val ADVANCED_DIAGNOSTICS = "advanced_diagnostics"
    private const val REVISION = "revision"

    fun read(context: Context): KeyboardSettingsSnapshot {
        val prefs = prefs(context)
        return KeyboardSettingsSnapshot(
            suggestions = prefs.getBoolean(SUGGESTIONS, true),
            autocorrect = prefs.getBoolean(AUTOCORRECT, true),
            learnNewWords = prefs.getBoolean(LEARN_NEW_WORDS, true),
            personalizedSuggestions = prefs.getBoolean(PERSONALIZED_SUGGESTIONS, true),
            hapticFeedback = prefs.getBoolean(HAPTIC_FEEDBACK, false),
            keypressSound = prefs.getBoolean(KEYPRESS_SOUND, false),
            keyPreview = prefs.getBoolean(KEY_PREVIEW, false),
            themeMode = KeyboardThemeMode.fromStoredValue(prefs.getString(THEME_MODE, null)),
            advancedDiagnostics = prefs.getBoolean(ADVANCED_DIAGNOSTICS, false),
            revision = prefs.getLong(REVISION, 0L),
        )
    }

    fun setSuggestions(context: Context, enabled: Boolean) =
        writeBoolean(context, SUGGESTIONS, enabled)

    fun setAutocorrect(context: Context, enabled: Boolean) =
        writeBoolean(context, AUTOCORRECT, enabled)

    fun setLearnNewWords(context: Context, enabled: Boolean) =
        writeBoolean(context, LEARN_NEW_WORDS, enabled)

    fun setPersonalizedSuggestions(context: Context, enabled: Boolean) =
        writeBoolean(context, PERSONALIZED_SUGGESTIONS, enabled)

    fun setHapticFeedback(context: Context, enabled: Boolean) =
        writeBoolean(context, HAPTIC_FEEDBACK, enabled)

    fun setKeypressSound(context: Context, enabled: Boolean) =
        writeBoolean(context, KEYPRESS_SOUND, enabled)

    fun setKeyPreview(context: Context, enabled: Boolean) =
        writeBoolean(context, KEY_PREVIEW, enabled)

    fun setAdvancedDiagnostics(context: Context, enabled: Boolean) =
        writeBoolean(context, ADVANCED_DIAGNOSTICS, enabled)

    fun setThemeMode(context: Context, mode: KeyboardThemeMode) {
        val prefs = prefs(context)
        prefs.edit()
            .putString(THEME_MODE, mode.storedValue)
            .putLong(REVISION, prefs.getLong(REVISION, 0L) + 1L)
            .apply()
    }

    private fun writeBoolean(context: Context, key: String, value: Boolean) {
        val prefs = prefs(context)
        prefs.edit()
            .putBoolean(key, value)
            .putLong(REVISION, prefs.getLong(REVISION, 0L) + 1L)
            .apply()
    }

    private fun prefs(context: Context) =
        (context.applicationContext ?: context).getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
}

/** Applies a forced light/dark resource configuration without hardcoded UI colors. */
object KeyboardThemeContext {
    fun effectiveNightMode(context: Context, mode: KeyboardThemeMode): Int = when (mode) {
        KeyboardThemeMode.SYSTEM ->
            context.resources.configuration.uiMode and Configuration.UI_MODE_NIGHT_MASK
        KeyboardThemeMode.LIGHT -> Configuration.UI_MODE_NIGHT_NO
        KeyboardThemeMode.DARK -> Configuration.UI_MODE_NIGHT_YES
    }

    fun wrap(context: Context, mode: KeyboardThemeMode): Context {
        if (mode == KeyboardThemeMode.SYSTEM) return context
        val configuration = Configuration(context.resources.configuration)
        val nightMode = effectiveNightMode(context, mode)
        configuration.uiMode =
            (configuration.uiMode and Configuration.UI_MODE_NIGHT_MASK.inv()) or nightMode
        return context.createConfigurationContext(configuration)
    }
}
