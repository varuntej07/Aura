package dev.varuntej.aura.assistant

import android.content.Intent
import android.speech.RecognitionService
import android.speech.SpeechRecognizer

/**
 * A voice-interaction service must declare a recognition service. Buddy does not provide
 * system speech recognition (it uses its own LiveKit pipeline), so this is a compliant
 * no-op stub that cleanly reports it cannot recognize rather than hanging a caller.
 */
class BuddyRecognitionService : RecognitionService() {
    override fun onStartListening(recognizerIntent: Intent?, listener: Callback?) {
        try {
            listener?.error(SpeechRecognizer.ERROR_CLIENT)
        } catch (_: Throwable) {
        }
    }

    override fun onCancel(listener: Callback?) {}

    override fun onStopListening(listener: Callback?) {}
}
