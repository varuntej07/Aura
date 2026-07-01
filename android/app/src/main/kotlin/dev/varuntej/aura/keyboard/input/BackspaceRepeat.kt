package dev.varuntej.aura.keyboard.input

/**
 * The accelerating delay schedule for holding backspace: a deliberate first repeat, then each
 * subsequent delete comes faster, down to a fast floor. Pure and unit-tested; the IME's backspace
 * touch handler owns the actual timing loop and the deletes.
 */
object BackspaceRepeat {

    /** The pause after the initial (tap) delete before auto-repeat begins. */
    const val INITIAL_DELAY_MS = 280L

    private const val START_DELAY_MS = 120L
    private const val MIN_DELAY_MS = 38L
    private const val STEP_MS = 8L

    /** The delay before the next repeat delete, given the repeat [tick] (0 = first repeat).
     *  Decreases by [STEP_MS] each tick and never drops below [MIN_DELAY_MS]. */
    fun delayForTick(tick: Int): Long {
        if (tick < 0) return START_DELAY_MS
        return (START_DELAY_MS - tick * STEP_MS).coerceAtLeast(MIN_DELAY_MS)
    }
}
