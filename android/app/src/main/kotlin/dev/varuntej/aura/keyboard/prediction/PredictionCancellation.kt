package dev.varuntej.aura.keyboard.prediction

/** Cooperative cancellation checked by bounded lexical and neural work. */
fun interface PredictionCancellation {
    fun isCancelled(): Boolean

    /** Installs reusable bounded-native-work termination without allocating a registration. */
    fun installCancellationCallback(callback: () -> Unit) = Unit

    /** Removes [callback] only if it is still the active native-work termination hook. */
    fun removeCancellationCallback(callback: () -> Unit) = Unit

    companion object {
        val NEVER = PredictionCancellation { false }
    }
}
