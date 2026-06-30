package dev.varuntej.aura.assistant

import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.service.voice.VoiceInteractionSession

/**
 * The assist session: when the gesture fires, open Buddy voice via the aura://voice deep
 * link and dismiss the (invisible) session immediately. Buddy shows no assist overlay of
 * its own; the app's existing voice surface is the UI.
 */
class BuddyVoiceInteractionSession(context: Context) : VoiceInteractionSession(context) {

    override fun onShow(args: Bundle?, showFlags: Int) {
        super.onShow(args, showFlags)
        launchBuddyVoice()
        hide()
    }

    private fun launchBuddyVoice() {
        try {
            context.startActivity(
                Intent(Intent.ACTION_VIEW, Uri.parse("aura://voice"))
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
            )
        } catch (t: Throwable) {
            // Nothing can handle the deep link; fail quietly (the user can open the app).
        }
    }
}
