package dev.varuntej.aura.keyboard.input

/** The three shift states a real keyboard has. */
enum class ShiftMode { NONE, SHIFTED, CAPS_LOCK }

/**
 * The shift key's state machine: NONE, one-shot SHIFTED, and sticky CAPS_LOCK. Pure and
 * unit-tested; the IME owns the double-tap timing and passes the result in.
 *
 * - A single tap toggles SHIFTED on/off (and turns CAPS_LOCK off).
 * - A double tap latches CAPS_LOCK until the next tap.
 * - Committing a letter consumes a one-shot SHIFTED (so only the first letter is capitalized);
 *   CAPS_LOCK is unaffected.
 * - Auto-capitalize sets SHIFTED at a sentence start, but never overrides CAPS_LOCK.
 */
class ShiftState {

    var mode: ShiftMode = ShiftMode.NONE
        private set

    /** Whether the next letter should be uppercase. */
    val isUpper: Boolean get() = mode != ShiftMode.NONE

    val isCapsLock: Boolean get() = mode == ShiftMode.CAPS_LOCK

    fun onShiftTap(doubleTap: Boolean) {
        mode = when {
            doubleTap -> ShiftMode.CAPS_LOCK
            mode == ShiftMode.NONE -> ShiftMode.SHIFTED
            else -> ShiftMode.NONE // a single tap clears SHIFTED or CAPS_LOCK
        }
    }

    /** Consume a one-shot SHIFTED after a letter is committed. */
    fun onTextCommitted() {
        if (mode == ShiftMode.SHIFTED) mode = ShiftMode.NONE
    }

    /** Auto-capitalize: SHIFTED at a sentence start, NONE otherwise. Never touches CAPS_LOCK. */
    fun applyAutoCap(shouldCapitalize: Boolean) {
        if (mode == ShiftMode.CAPS_LOCK) return
        mode = if (shouldCapitalize) ShiftMode.SHIFTED else ShiftMode.NONE
    }

    fun reset() {
        mode = ShiftMode.NONE
    }
}
