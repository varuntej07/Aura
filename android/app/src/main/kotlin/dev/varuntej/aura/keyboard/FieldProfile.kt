package dev.varuntej.aura.keyboard

import android.text.InputType
import android.view.inputmethod.EditorInfo

/** Which key grid to render for the focused field. */
enum class KeyLayout { QWERTY, EMAIL, URL, NUMERIC, PHONE, PIN }

/**
 * What the keyboard should do for the field the cursor is in, derived once per focus
 * from the field's [EditorInfo]. Pure and unit-testable: [BuddyImeService] reads it to
 * pick the key layout, show or hide the Buddy bar, and tag the draft request.
 *
 * The privacy invariant lives here. A secure field (password / OTP / PIN) has
 * [isSecure] = true, which forces [memoryActionsAllowed] = false, so reply / continue /
 * rewrite never run and nothing typed in a secure field is ever sent to the backend.
 * A text-password field still offers [passwordGenerate] (a local, offline strong
 * password chip) which commits a value straight into the field and sends nothing
 * anywhere; the OS autofill owns the "Save password?" prompt.
 */
data class FieldProfile(
    val layout: KeyLayout,
    val showBuddyBar: Boolean,
    val memoryActionsAllowed: Boolean,
    val passwordGenerate: Boolean,
    val isSecure: Boolean,
    val fieldTypeWire: String,
    // The app set TYPE_TEXT_FLAG_NO_SUGGESTIONS on this field ("don't offer suggestions"): forces
    // the prediction strip + autocorrect off even on an otherwise normal QWERTY field.
    val suggestionsOptedOut: Boolean = false,
    // The app set IME_FLAG_NO_PERSONALIZED_LEARNING ("don't persist what is typed here"): forces
    // learn-on-commit off so a recovery code / 2FA value typed in plain text is never remembered.
    val learningOptedOut: Boolean = false,
) {
    /**
     * Whether on-device typing intelligence (the composing-text pipeline + suggestion strip)
     * may run for this field. Derived, never stored, so the privacy gate can never drift from
     * the layout/secure facts above: a word is only ever composed, predicted, or learned in a
     * non-secure prose field. OFF for every numeric/phone/PIN/password field (a PIN must never
     * be composed or learned), so a secure field keeps the exact commit-per-keystroke path.
     */
    val predictionsAllowed: Boolean
        get() = !isSecure && !suggestionsOptedOut && layout in PREDICTION_LAYOUTS

    /**
     * Whether autocorrect-on-separator (and the spell squiggle) may run. A strict subset of
     * [predictionsAllowed]: OFF for email and url, where "correcting" an address would corrupt
     * it, mirroring how a real keyboard suppresses autocorrect in those fields.
     */
    val autocorrectAllowed: Boolean
        get() = predictionsAllowed && layout == KeyLayout.QWERTY

    /**
     * Whether a committed word may be learned into the personal dictionary. Tied to
     * [autocorrectAllowed]: we only learn from real prose (QWERTY text), never from an email
     * address, a url, a password, or any secure field.
     */
    val learningAllowed: Boolean
        get() = autocorrectAllowed && !learningOptedOut

    companion object {
        // The layouts that carry prose worth predicting. Numeric / phone / PIN are absent, so
        // a digit pad never composes; password text is QWERTY but excluded via isSecure above.
        private val PREDICTION_LAYOUTS = setOf(KeyLayout.QWERTY, KeyLayout.EMAIL, KeyLayout.URL)

        /** The safe default for a plain text field (and for a null EditorInfo). */
        fun text(): FieldProfile = FieldProfile(
            layout = KeyLayout.QWERTY,
            showBuddyBar = true,
            memoryActionsAllowed = true,
            passwordGenerate = false,
            isSecure = false,
            fieldTypeWire = "text",
        )

        fun fromEditorInfo(info: EditorInfo?): FieldProfile =
            if (info == null) text() else fromInputType(info.inputType, info.imeOptions)

        /** The pure core, split out so it is unit-testable without an EditorInfo (which
         *  needs the Android framework). [InputType] / [EditorInfo] flag constants are
         *  compile-time ints, so this stays runnable on the plain JVM. */
        fun fromInputType(type: Int, imeOptions: Int = 0): FieldProfile {
            val klass = type and InputType.TYPE_MASK_CLASS
            val variation = type and InputType.TYPE_MASK_VARIATION
            val base = when (klass) {
                InputType.TYPE_CLASS_NUMBER ->
                    if (variation == InputType.TYPE_NUMBER_VARIATION_PASSWORD) {
                        secure(KeyLayout.PIN, "password")
                    } else {
                        utility(KeyLayout.NUMERIC, "number")
                    }
                InputType.TYPE_CLASS_PHONE -> utility(KeyLayout.PHONE, "phone")
                InputType.TYPE_CLASS_DATETIME -> utility(KeyLayout.NUMERIC, "datetime")
                InputType.TYPE_CLASS_TEXT -> when (variation) {
                    InputType.TYPE_TEXT_VARIATION_PASSWORD,
                    InputType.TYPE_TEXT_VARIATION_VISIBLE_PASSWORD,
                    InputType.TYPE_TEXT_VARIATION_WEB_PASSWORD,
                    -> passwordText()
                    InputType.TYPE_TEXT_VARIATION_EMAIL_ADDRESS,
                    InputType.TYPE_TEXT_VARIATION_WEB_EMAIL_ADDRESS,
                    -> utility(KeyLayout.EMAIL, "email")
                    InputType.TYPE_TEXT_VARIATION_URI -> utility(KeyLayout.URL, "url")
                    else -> text()
                }
                else -> text()
            }
            // Honor the app's "don't suggest" / "don't persist" requests on top of the field type:
            // NO_SUGGESTIONS lives in inputType, NO_PERSONALIZED_LEARNING in imeOptions. A secure
            // field already suppresses everything, so this only ever tightens a non-secure field.
            val suggestionsOptedOut = (type and InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS) != 0
            val learningOptedOut =
                (imeOptions and EditorInfo.IME_FLAG_NO_PERSONALIZED_LEARNING) != 0
            return if (suggestionsOptedOut || learningOptedOut) {
                base.copy(
                    suggestionsOptedOut = suggestionsOptedOut,
                    learningOptedOut = learningOptedOut,
                )
            } else {
                base
            }
        }

        // A numeric / phone / email / url field: a fitted layout, no Buddy bar, no
        // memory, no password chip. (An email or url address is not prose to draft.)
        private fun utility(layout: KeyLayout, wire: String) = FieldProfile(
            layout = layout,
            showBuddyBar = false,
            memoryActionsAllowed = false,
            passwordGenerate = false,
            isSecure = false,
            fieldTypeWire = wire,
        )

        // A secure numeric field (PIN / numeric OTP): a digit pad, nothing else. No
        // generate, because a numeric PIN is chosen by the service, not the user.
        private fun secure(layout: KeyLayout, wire: String) = FieldProfile(
            layout = layout,
            showBuddyBar = false,
            memoryActionsAllowed = false,
            passwordGenerate = false,
            isSecure = true,
            fieldTypeWire = wire,
        )

        // A text-password field: QWERTY, no memory bar, but a single offline
        // "Generate strong password" chip in the bar slot.
        private fun passwordText() = FieldProfile(
            layout = KeyLayout.QWERTY,
            showBuddyBar = true,
            memoryActionsAllowed = false,
            passwordGenerate = true,
            isSecure = true,
            fieldTypeWire = "password",
        )
    }
}
