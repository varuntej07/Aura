package dev.varuntej.aura.assistant

import android.os.Bundle
import android.service.voice.VoiceInteractionSession
import android.service.voice.VoiceInteractionSessionService

/** Creates a Buddy assist session each time the assist gesture fires. */
class BuddyVoiceInteractionSessionService : VoiceInteractionSessionService() {
    override fun onNewSession(args: Bundle?): VoiceInteractionSession =
        BuddyVoiceInteractionSession(this)
}
