package dev.varuntej.aura.keyboard

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import androidx.core.content.ContextCompat
import io.livekit.android.AudioOptions
import io.livekit.android.LiveKit
import io.livekit.android.LiveKitOverrides
import io.livekit.android.events.RoomEvent
import io.livekit.android.events.collect
import io.livekit.android.room.Room
import io.livekit.android.room.participant.RemoteParticipant
import io.livekit.android.room.track.DataPublishReliability
import io.livekit.android.room.track.RemoteAudioTrack
import kotlinx.coroutines.CoroutineExceptionHandler
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.coroutines.withContext
import kotlin.coroutines.resume
import org.json.JSONObject
import java.io.BufferedReader
import java.net.HttpURLConnection
import java.net.URL

/**
 * Talk to Buddy IN-PROCESS from the keyboard: a native LiveKit/WebRTC duplex to the same
 * tuned voice agent the app uses, without leaving the host app.
 *
 * Flow: mint a fresh Firebase token in the IME process -> GET /voice/token -> connect to
 * the LiveKit room -> mic on -> publish the on-screen text as a screen_context data
 * message so Buddy can talk about what's on screen -> stream audio both ways. Buddy's
 * audio plays through the device automatically; the user's mic is captured in-process.
 *
 * Uses io.github.webrtc-sdk:android-prefixed (separate from flutter_webrtc), so it cannot
 * disturb the app's own voice. Every failure resolves to a coded state so the keyboard
 * shows a bounded message and never hangs; on init failure the caller deep-links to the
 * app instead (the low-memory / unsupported-device fallback).
 */
class KeyboardVoiceController(private val appContext: Context) {

    enum class State { CONNECTING, LISTENING, SPEAKING, ERROR, ENDED, NO_MIC, NO_CREDENTIAL }

    // These are touched from coroutines on Dispatchers.Main plus the IO hop and the public
    // [isActive] getter; @Volatile gives every reader a consistent, published view.
    @Volatile
    private var room: Room? = null
    private var scope: CoroutineScope? = null
    @Volatile
    private var onState: ((State, String?) -> Unit)? = null
    // Live captions: (fromBuddy, text, isFinal, segmentId). Fired on the main thread as
    // LiveKit transcription segments arrive, the same stream the app's subtitles read.
    @Volatile
    private var onTranscript: ((Boolean, String, Boolean, String) -> Unit)? = null
    @Volatile
    private var sawRemoteAudio = false

    val isActive: Boolean get() = room != null

    /** Start a voice turn. [onState] and [onTranscript] are always invoked on the main thread. */
    fun start(
        baseUrl: String,
        screenContext: JSONObject?,
        onState: (State, String?) -> Unit,
        onTranscript: (fromBuddy: Boolean, text: String, isFinal: Boolean, segmentId: String) -> Unit,
    ) {
        if (room != null) return
        this.onState = onState
        this.onTranscript = onTranscript
        if (ContextCompat.checkSelfPermission(appContext, Manifest.permission.RECORD_AUDIO)
            != PackageManager.PERMISSION_GRANTED
        ) {
            onState(State.NO_MIC, null)
            return
        }
        onState(State.CONNECTING, null)
        // Any uncaught exception in a child coroutine would crash the HOST (IME) process by
        // default ("Aura keeps stopping"). This guard turns every such failure into a benign
        // error state instead, so voice can never take the keyboard down.
        val crashGuard = CoroutineExceptionHandler { _, error ->
            onState(State.ERROR, error.message)
        }
        val s = CoroutineScope(Dispatchers.Main + SupervisorJob() + crashGuard)
        scope = s
        s.launch {
            val firebaseToken = awaitFreshIdToken()
            if (firebaseToken.isNullOrBlank()) {
                onState(State.NO_CREDENTIAL, null)
                return@launch
            }
            val lk = withContext(Dispatchers.IO) { fetchVoiceToken(baseUrl, firebaseToken) }
            if (lk == null) {
                onState(State.ERROR, "token")
                return@launch
            }
            val r = try {
                // Replace LiveKit's default AudioSwitchHandler (which pulls a conflicting
                // audioswitch fork that crashes the IME) with our minimal handler.
                LiveKit.create(
                    appContext,
                    overrides = LiveKitOverrides(
                        audioOptions = AudioOptions(audioHandler = KeyboardAudioHandler(appContext)),
                    ),
                )
            } catch (t: Throwable) {
                onState(State.ERROR, t.message)
                return@launch
            }
            room = r
            s.launch {
                try {
                    r.events.collect { onRoomEvent(it, r) }
                } catch (_: Throwable) {
                    // Event stream ended or failed; connect/watchdog already report state.
                }
            }
            try {
                r.connect(lk.url, lk.token)
                r.localParticipant.setMicrophoneEnabled(true)
                screenContext?.let {
                    r.localParticipant.publishData(
                        it.toString().toByteArray(Charsets.UTF_8),
                        DataPublishReliability.RELIABLE,
                    )
                }
                onState(State.LISTENING, null)
                // No-agent watchdog: Buddy greets within a few seconds of joining, so if no
                // remote audio arrives in 20s the worker isn't there. Surface it instead of
                // a forever-"Listening".
                s.launch {
                    delay(20_000)
                    if (room != null && !sawRemoteAudio) onState(State.ERROR, "no_agent")
                }
            } catch (t: Throwable) {
                onState(State.ERROR, t.message)
                stop()
            }
        }
    }

    // RoomEvent.TranscriptionReceived (live captions) is still @Beta in livekit-android; the
    // app already relies on the same stream, so we opt in here too.
    @OptIn(io.livekit.android.annotations.Beta::class)
    private fun onRoomEvent(event: RoomEvent, r: Room) {
        when (event) {
            is RoomEvent.TrackSubscribed -> {
                if (event.track is RemoteAudioTrack) sawRemoteAudio = true
            }
            is RoomEvent.ActiveSpeakersChanged -> {
                val agentSpeaking = event.speakers.any { it is RemoteParticipant }
                if (agentSpeaking) sawRemoteAudio = true
                onState?.invoke(if (agentSpeaking) State.SPEAKING else State.LISTENING, null)
            }
            is RoomEvent.TranscriptionReceived -> {
                // A RemoteParticipant is Buddy; the local participant is the user. The agent
                // publishes both sides of the transcript into the room (same as the app).
                val fromBuddy = event.participant is RemoteParticipant
                if (fromBuddy) sawRemoteAudio = true
                val callback = onTranscript
                if (callback != null) {
                    for (segment in event.transcriptionSegments) {
                        callback(fromBuddy, segment.text, segment.`final`, segment.id)
                    }
                }
            }
            is RoomEvent.Disconnected -> onState?.invoke(State.ENDED, null)
            else -> {}
        }
    }

    /** Disconnect and release everything. Safe to call repeatedly. */
    fun stop() {
        val r = room
        room = null
        sawRemoteAudio = false
        try {
            r?.disconnect()
        } catch (_: Throwable) {
        }
        try {
            scope?.cancel()
        } catch (_: Throwable) {
        }
        scope = null
    }

    private suspend fun awaitFreshIdToken(): String? = suspendCancellableCoroutine { cont ->
        KeyboardAuth.freshIdToken { token ->
            if (cont.isActive) cont.resume(token)
        }
    }

    /** GET {baseUrl}/voice/token -> the LiveKit url + token, or null on any failure.
     *  surface=keyboard tells the voice worker this is a quick tap from the keyboard, so
     *  Buddy keeps replies short and task-focused (see voice_agent._resolve_surface). */
    private fun fetchVoiceToken(baseUrl: String, idToken: String): LkToken? {
        val endpoint = baseUrl.trimEnd('/') + "/voice/token?surface=keyboard"
        val conn = (URL(endpoint).openConnection() as HttpURLConnection).apply {
            requestMethod = "GET"
            connectTimeout = 8_000
            readTimeout = 9_000
            setRequestProperty("Authorization", "Bearer $idToken")
        }
        return try {
            if (conn.responseCode != HttpURLConnection.HTTP_OK) return null
            val text = conn.inputStream.bufferedReader().use(BufferedReader::readText)
            val json = JSONObject(text)
            val url = json.optString("url")
            val token = json.optString("token")
            if (url.isBlank() || token.isBlank()) null else LkToken(url, token)
        } catch (t: Throwable) {
            null
        } finally {
            conn.disconnect()
        }
    }

    private data class LkToken(val url: String, val token: String)
}
