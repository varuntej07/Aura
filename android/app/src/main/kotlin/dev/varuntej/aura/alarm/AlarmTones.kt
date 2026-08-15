package dev.varuntej.aura.alarm

import android.content.Context
import android.content.res.AssetFileDescriptor
import android.media.RingtoneManager
import android.net.Uri
import android.util.Log

/**
 * The sounds an alarm can ring with, and the only place that decides which one.
 *
 * Mirrors `lib/core/constants/alarm_tones.dart` and
 * `backend/src/services/alarm_tones.py`. This is the copy that actually makes
 * noise at 3 AM, in a process with no Flutter engine and possibly no network,
 * which is why the table is duplicated here rather than read from Dart.
 *
 * Clips are bundled as FLUTTER assets, not `res/raw`. That is deliberate: the
 * settings picker previews `assets/alarm_tones/<slug>.wav` through just_audio
 * and this reads the same bytes out of the same APK entry, so the sound someone
 * chose and the sound that wakes them cannot drift apart.
 *
 * These clips do not declare beat metadata, so [AlarmRippleView] uses its
 * ambient timing while the audio loops.
 */
object AlarmTones {

    private const val TAG = "AuraAlarm"

    /** Buddy reads the reminder aloud over [BED_SLUG]. */
    const val BUDDY = "buddy"

    /** A sound from the user's own device, chosen with the system picker. */
    const val DEVICE = "device"

    /** What Buddy's voice rings over, and what an unknown slug degrades to. */
    const val BED_SLUG = "morning-clock-alarm"

    /** No beat this build knows about: the screen falls back to ambient ripples. */
    const val AMBIENT_BEAT_MS = 3200L

    data class Tone(val slug: String, val beatPeriodMs: Long = AMBIENT_BEAT_MS) {
        val assetPath: String get() = "flutter_assets/assets/alarm_tones/$slug.wav"
    }

    private val BUNDLED = listOf(
        Tone("morning-clock-alarm"),
        Tone("alert-alarm"),
        Tone("buzzer-alarm"),
        Tone("warning-buzzer"),
        Tone("street-public-alarm"),
        Tone("battleship-alarm"),
        Tone("retro-game-emergency"),
        Tone("rooster-crowing"),
        Tone("short-rooster-crowing"),
    ).associateBy { it.slug }

    /**
     * The clip a slug should play, or null when it is not a bundled tone.
     *
     * [BUDDY] resolves to the bed clip rather than to null: the spoken line is
     * layered on top by [AlarmService], and if the clip never arrives the bed is
     * what keeps the alarm from being silent.
     */
    fun bundled(slug: String): Tone? = when (slug) {
        BUDDY -> BUNDLED[BED_SLUG]
        else -> BUNDLED[slug]
    }

    /**
     * Milliseconds between ripples for a slug.
     *
     * Device-picked sounds and the system default have no BPM this process can
     * know, so they get the ambient period instead of a wrong one.
     */
    fun beatPeriodMs(slug: String): Long = bundled(slug)?.beatPeriodMs ?: AMBIENT_BEAT_MS

    /**
     * Open a bundled tone for playback, or null if it is not bundled or the APK
     * entry cannot be read.
     *
     * Returning null is a normal outcome, not an error: every caller falls
     * through to the device URI and then to the system default alarm sound.
     */
    fun openAsset(context: Context, slug: String): AssetFileDescriptor? {
        val tone = bundled(slug) ?: return null
        return try {
            context.assets.openFd(tone.assetPath)
        } catch (t: Throwable) {
            // Reachable if a build ships a slug whose asset was not bundled.
            // Loud, because it is silent-alarm territory and the fallback below
            // would otherwise hide it completely.
            Log.e(TAG, "alarm tone asset missing: ${tone.assetPath}", t)
            null
        }
    }

    /**
     * The URI picked with the system ringtone picker, with no fallback folded
     * into it. The service must know whether this exact source opened before it
     * records `device` as the ripple timing slug.
     */
    fun deviceUri(context: Context): Uri? {
        val stored = AlarmStore.deviceToneUri(context)
        if (stored.isNullOrBlank()) return null
        return runCatching { Uri.parse(stored) }.getOrNull()
    }

    /** Whether this slug is safe to persist as a global alarm preference. */
    fun isSelectable(slug: String): Boolean =
        slug.isEmpty() || slug == DEVICE || slug == BUDDY || BUNDLED.containsKey(slug)

    /** Exactly what the alarm played before tones existed. The last resort. */
    fun systemDefaultUri(): Uri? =
        RingtoneManager.getDefaultUri(RingtoneManager.TYPE_ALARM)
            ?: RingtoneManager.getDefaultUri(RingtoneManager.TYPE_RINGTONE)

    /**
     * The name of the user's picked sound, for the settings row. Blank if none.
     *
     * Resolved lazily rather than stored alongside the URI so a renamed or
     * deleted track shows the truth instead of a label kept from months ago.
     */
    fun deviceToneTitle(context: Context): String {
        val stored = AlarmStore.deviceToneUri(context)
        if (stored.isNullOrBlank()) return ""
        return try {
            RingtoneManager.getRingtone(context, Uri.parse(stored))?.getTitle(context).orEmpty()
        } catch (t: Throwable) {
            Log.w(TAG, "could not read the device tone title", t)
            ""
        }
    }
}
