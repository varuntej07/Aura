package dev.varuntej.aura.keyboard

import android.content.Context
import android.media.AudioAttributes
import android.media.AudioFocusRequest
import android.media.AudioManager
import android.os.Build

/**
 * A minimal LiveKit [io.livekit.android.audio.AudioHandler] for in-keyboard voice.
 *
 * LiveKit's default handler (AudioSwitchHandler) pulls the davidliu/audioswitch fork to do
 * Bluetooth/wired device switching; in this app that fork resolves (via the Flutter
 * livekit_client) to a commit missing `CommDeviceAudioSwitch`, which crashes the IME with a
 * NoClassDefFoundError. We don't need device-switching in the keyboard, so we replace the
 * handler entirely: just claim communication audio mode + voice-comm focus on start and
 * restore on stop. No audioswitch dependency is ever touched.
 *
 * Routes to speakerphone by default (the user is looking at the screen, not holding the
 * phone to their ear). All audio-state changes are reverted in [stop] so a voice turn never
 * leaves the device stuck in communication mode.
 */
class KeyboardAudioHandler(context: Context) : io.livekit.android.audio.AudioHandler {

    private val audioManager =
        context.applicationContext.getSystemService(Context.AUDIO_SERVICE) as AudioManager

    private var savedMode = AudioManager.MODE_NORMAL
    private var savedSpeakerphone = false
    private var focusRequest: AudioFocusRequest? = null

    override fun start() {
        savedMode = audioManager.mode
        @Suppress("DEPRECATION")
        savedSpeakerphone = audioManager.isSpeakerphoneOn

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val request = AudioFocusRequest.Builder(AudioManager.AUDIOFOCUS_GAIN_TRANSIENT)
                .setAudioAttributes(
                    AudioAttributes.Builder()
                        .setUsage(AudioAttributes.USAGE_VOICE_COMMUNICATION)
                        .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                        .build(),
                )
                .build()
            focusRequest = request
            runCatching { audioManager.requestAudioFocus(request) }
        } else {
            @Suppress("DEPRECATION")
            runCatching {
                audioManager.requestAudioFocus(
                    null,
                    AudioManager.STREAM_VOICE_CALL,
                    AudioManager.AUDIOFOCUS_GAIN_TRANSIENT,
                )
            }
        }

        runCatching { audioManager.mode = AudioManager.MODE_IN_COMMUNICATION }
        @Suppress("DEPRECATION")
        runCatching { audioManager.isSpeakerphoneOn = true }
    }

    override fun stop() {
        @Suppress("DEPRECATION")
        runCatching { audioManager.isSpeakerphoneOn = savedSpeakerphone }
        runCatching { audioManager.mode = savedMode }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            focusRequest?.let { request -> runCatching { audioManager.abandonAudioFocusRequest(request) } }
        } else {
            @Suppress("DEPRECATION")
            runCatching { audioManager.abandonAudioFocus(null) }
        }
        focusRequest = null
    }
}
