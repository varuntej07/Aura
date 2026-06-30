package dev.varuntej.aura.keyboard

import android.text.InputType
import android.view.inputmethod.EditorInfo
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Pure-JVM coverage for the field detection that drives the dynamic keyboard. Uses
 * [FieldProfile.fromInputType] so no EditorInfo (and no Android framework) is needed;
 * the InputType constants are compile-time ints read straight from android.jar.
 */
class FieldProfileTest {

    private fun profile(type: Int) = FieldProfile.fromInputType(type)

    @Test
    fun plainText_showsBar_allowsMemory() {
        val p = profile(InputType.TYPE_CLASS_TEXT)
        assertEquals(KeyLayout.QWERTY, p.layout)
        assertTrue(p.showBuddyBar)
        assertTrue(p.memoryActionsAllowed)
        assertFalse(p.passwordGenerate)
        assertFalse(p.isSecure)
        assertEquals("text", p.fieldTypeWire)
    }

    @Test
    fun plainText_allowsPrediction_autocorrect_andLearning() {
        val p = profile(InputType.TYPE_CLASS_TEXT)
        assertTrue(p.predictionsAllowed)
        assertTrue(p.autocorrectAllowed)
        assertTrue(p.learningAllowed)
    }

    @Test
    fun email_predictsButNeverAutocorrectsOrLearns() {
        // Prediction can help with common words, but "correcting" or learning an address
        // would corrupt it, so autocorrect + learning stay off for email/url.
        val p = profile(InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_EMAIL_ADDRESS)
        assertTrue(p.predictionsAllowed)
        assertFalse(p.autocorrectAllowed)
        assertFalse(p.learningAllowed)
    }

    @Test
    fun url_predictsButNeverAutocorrectsOrLearns() {
        val p = profile(InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_URI)
        assertTrue(p.predictionsAllowed)
        assertFalse(p.autocorrectAllowed)
        assertFalse(p.learningAllowed)
    }

    @Test
    fun textPassword_neverPredicts_autocorrects_orLearns() {
        // The privacy invariant: a secure field is opaque to every typing-intelligence path.
        val p = profile(InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_PASSWORD)
        assertFalse(p.predictionsAllowed)
        assertFalse(p.autocorrectAllowed)
        assertFalse(p.learningAllowed)
    }

    @Test
    fun numeric_phone_pin_neverPredict() {
        val numeric = profile(InputType.TYPE_CLASS_NUMBER)
        val phone = profile(InputType.TYPE_CLASS_PHONE)
        val pin = profile(InputType.TYPE_CLASS_NUMBER or InputType.TYPE_NUMBER_VARIATION_PASSWORD)
        for (p in listOf(numeric, phone, pin)) {
            assertFalse(p.predictionsAllowed)
            assertFalse(p.autocorrectAllowed)
            assertFalse(p.learningAllowed)
        }
    }

    @Test
    fun textPassword_suppressesMemory_offersGenerate() {
        val p = profile(InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_PASSWORD)
        assertEquals(KeyLayout.QWERTY, p.layout)
        assertFalse(p.memoryActionsAllowed)   // never draft in a password field
        assertTrue(p.passwordGenerate)
        assertTrue(p.isSecure)
        assertEquals("password", p.fieldTypeWire)
    }

    @Test
    fun visibleAndWebPassword_areAlsoSecure() {
        for (variation in listOf(
            InputType.TYPE_TEXT_VARIATION_VISIBLE_PASSWORD,
            InputType.TYPE_TEXT_VARIATION_WEB_PASSWORD,
        )) {
            val p = profile(InputType.TYPE_CLASS_TEXT or variation)
            assertTrue(p.isSecure)
            assertTrue(p.passwordGenerate)
            assertFalse(p.memoryActionsAllowed)
        }
    }

    @Test
    fun email_getsEmailLayout_noBar() {
        val p = profile(InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_EMAIL_ADDRESS)
        assertEquals(KeyLayout.EMAIL, p.layout)
        assertFalse(p.showBuddyBar)
        assertFalse(p.memoryActionsAllowed)
        assertEquals("email", p.fieldTypeWire)
    }

    @Test
    fun uri_getsUrlLayout_noBar() {
        val p = profile(InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_URI)
        assertEquals(KeyLayout.URL, p.layout)
        assertFalse(p.showBuddyBar)
        assertEquals("url", p.fieldTypeWire)
    }

    @Test
    fun number_getsNumericPad_noBar() {
        val p = profile(InputType.TYPE_CLASS_NUMBER)
        assertEquals(KeyLayout.NUMERIC, p.layout)
        assertFalse(p.showBuddyBar)
        assertFalse(p.memoryActionsAllowed)
        assertEquals("number", p.fieldTypeWire)
    }

    @Test
    fun numberPassword_getsPinPad_secure_noGenerate() {
        val p = profile(InputType.TYPE_CLASS_NUMBER or InputType.TYPE_NUMBER_VARIATION_PASSWORD)
        assertEquals(KeyLayout.PIN, p.layout)
        assertTrue(p.isSecure)
        assertFalse(p.passwordGenerate)       // a numeric PIN is not user-chosen
        assertFalse(p.showBuddyBar)
    }

    @Test
    fun phone_getsDialpad() {
        val p = profile(InputType.TYPE_CLASS_PHONE)
        assertEquals(KeyLayout.PHONE, p.layout)
        assertFalse(p.showBuddyBar)
        assertEquals("phone", p.fieldTypeWire)
    }

    @Test
    fun datetime_getsNumericPad() {
        val p = profile(InputType.TYPE_CLASS_DATETIME)
        assertEquals(KeyLayout.NUMERIC, p.layout)
        assertFalse(p.showBuddyBar)
        assertEquals("datetime", p.fieldTypeWire)
    }

    @Test
    fun noSuggestionsFlag_suppressesPredictionAutocorrectAndLearning() {
        // A plain text field that asked for no suggestions (e.g. a 2FA / recovery-code box typed in
        // clear text) must not predict, autocorrect, or learn, even though it is QWERTY text.
        val p = profile(InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS)
        assertEquals(KeyLayout.QWERTY, p.layout)
        assertFalse(p.predictionsAllowed)
        assertFalse(p.autocorrectAllowed)
        assertFalse(p.learningAllowed)
    }

    @Test
    fun noPersonalizedLearningFlag_keepsPredictionButBlocksLearning() {
        // NO_PERSONALIZED_LEARNING (imeOptions) means "don't persist what is typed here": the
        // strip + autocorrect may still help, but nothing is ever written to the personal dict.
        val p = FieldProfile.fromInputType(
            InputType.TYPE_CLASS_TEXT,
            EditorInfo.IME_FLAG_NO_PERSONALIZED_LEARNING,
        )
        assertTrue(p.predictionsAllowed)
        assertTrue(p.autocorrectAllowed)
        assertFalse(p.learningAllowed)
    }

    @Test
    fun unknownClass_fallsBackToText() {
        // A class we do not special-case degrades to the safe plain-text profile.
        val p = profile(InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_PERSON_NAME)
        assertEquals(KeyLayout.QWERTY, p.layout)
        assertTrue(p.memoryActionsAllowed)
        assertEquals("text", p.fieldTypeWire)
    }
}
