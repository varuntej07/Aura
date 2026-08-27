package dev.varuntej.aura.keyboard

import android.content.Context
import java.io.File

/**
 * Every SharedPreferences store the Buddy Keyboard owns that holds data derived from the user.
 *
 * This list exists so "Clear learned words and personalization" has one place to enumerate, and
 * so adding a new keyboard store is a change that lands next to the deletion path rather than
 * silently outliving it. A deletion control that reports success while leaving identifiable
 * derived data on disk is the failure this guards against.
 *
 * Behaviour-only preferences ([dev.varuntej.aura.keyboard.settings.KeyboardSettingsStore]) and
 * the consent record ([dev.varuntej.aura.keyboard.settings.KeyboardConsentStore]) are NOT here
 * on purpose: they hold no typed text or learned vocabulary, and wiping someone's theme choice
 * or re-asking for consent they already answered is not what "clear my learned words" means.
 */
internal object KeyboardOwnedStores {

    /** Cloud-derived vocabulary hints (names, interests) fetched from GET /keyboard/vocab. */
    const val VOCAB_HINTS_PREFS = "buddy_keyboard_vocab"

    /** Recently used emoji. Small, but it is a record of what this person types. */
    const val EMOJI_PREFS = "buddy_kb_emoji"

    private val ALL = listOf(VOCAB_HINTS_PREFS, EMOJI_PREFS)

    /**
     * Clear each store's contents synchronously, then remove its backing file.
     *
     * The clear-then-delete order matters. A live SharedPreferences instance holds its values in
     * memory and can rewrite the file after a bare delete, and `deleteSharedPreferences` itself
     * is documented to fail while the store is in use. `commit()` (not `apply()`) is used so the
     * write is on disk before [remainingFiles] is asked to prove the removal.
     */
    fun clearAll(context: Context) {
        val appContext = context.applicationContext ?: context
        for (name in ALL) {
            try {
                appContext.getSharedPreferences(name, Context.MODE_PRIVATE).edit().clear().commit()
            } catch (_: Throwable) {
                // Fall through to the file delete; remainingFiles() is what decides success.
            }
            backingFile(appContext, name).takeIf(File::exists)?.delete()
        }
    }

    /** The stores that survived, so the caller can refuse to report success. */
    fun remainingFiles(context: Context): List<File> {
        val appContext = context.applicationContext ?: context
        return ALL.map { backingFile(appContext, it) }.filter(File::exists)
    }

    private fun backingFile(context: Context, name: String): File =
        File(File(context.applicationInfo.dataDir, "shared_prefs"), "$name.xml")
}
