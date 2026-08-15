package dev.varuntej.aura.alarm

import android.content.Context
import android.content.SharedPreferences
import org.json.JSONArray
import org.json.JSONObject

/**
 * The device's own record of what is armed, what already rang, and what the user
 * did about it.
 *
 * Plain SharedPreferences, and NOT anything on the Dart side, because of when
 * this is read: an alarm fires from a BroadcastReceiver at 3 AM in a process
 * that may have been started for that broadcast alone, with no Flutter engine,
 * no Firebase, and no network. Anything that needs Dart to be alive is unusable
 * at exactly the moment that matters.
 *
 * Not encrypted: this holds alarm times and a short message the user wrote for
 * themselves, all of which the notification is about to display on the lock
 * screen anyway. EncryptedSharedPreferences costs an AndroidKeyStore round trip
 * on a path that runs while the device is waking up.
 */
object AlarmStore {

    private const val PREFS = "aura_alarms"

    /** reminder_id -> the armed schedule, as JSON. Holds server and local entries. */
    private const val KEY_ARMED = "armed"

    /** The Settings-owned device-local regular wake-up definition. */
    private const val KEY_REGULAR_ALARM = "regular_alarm"

    /** reminder_id -> epoch millis it rang. Suppresses duplicate rings. */
    private const val KEY_FIRED = "fired"
    private const val FLUTTER_PREFS = "FlutterSharedPreferences"
    private const val FLUTTER_KEY_FIRED = "flutter.alarm_fired_occurrences"

    /** Acks taken while offline or with no app running, flushed by Dart later. */
    private const val KEY_PENDING_ACKS = "pending_acks"

    /** When the exact-alarm permission prompt was last shown, epoch millis. */
    private const val KEY_PROMPTED_AT = "permission_prompted_at"

    /**
     * Reminder ids currently armed INEXACTLY, because the permission was refused.
     * These fire late under Doze, so the app describes them differently and
     * telemetry counts them apart from real alarms.
     */
    private const val KEY_INEXACT = "inexact"

    /** The system-picker ringtone URI, when the user chose the `device` tone. */
    private const val KEY_DEVICE_TONE_URI = "device_tone_uri"

    /** Firestore remains authoritative; this is its offline native mirror. */
    private const val KEY_DEFAULT_TONE = "default_tone"

    /** Epoch millis the audio for the current ring started. See [markRingStarted]. */
    private const val KEY_RING_STARTED_AT = "ring_started_at"

    /** The tone slug currently sounding, so the ripples know its beat. */
    private const val KEY_RING_TONE = "ring_tone"

    /**
     * How long a fired alarm stays in the ledger. Long enough to cover the
     * backstop push (which arrives seconds after the local ring) and a retry
     * storm; short enough that a daily alarm at the same id is never suppressed.
     */
    const val FIRED_LEDGER_TTL_MS = 60L * 60L * 1000L

    private fun prefs(context: Context): SharedPreferences =
        context.applicationContext.getSharedPreferences(PREFS, Context.MODE_PRIVATE)

    // Armed schedule -------------------------------------------------------

    fun armed(context: Context): Map<String, AlarmSchedule> {
        val raw = prefs(context).getString(KEY_ARMED, null) ?: return emptyMap()
        val out = mutableMapOf<String, AlarmSchedule>()
        try {
            val root = JSONObject(raw)
            for (id in root.keys()) {
                AlarmSchedule.fromJson(root.getJSONObject(id))?.let { out[id] = it }
            }
        } catch (_: Throwable) {
            // A corrupt blob must not brick alarms forever. Dropping it means the
            // next reconcile re-arms everything from the server.
            return emptyMap()
        }
        return out
    }

    fun putArmed(context: Context, schedule: AlarmSchedule) {
        val current = armed(context).toMutableMap()
        current[schedule.reminderId] = schedule
        writeArmed(context, current)
    }

    fun removeArmed(context: Context, reminderId: String) {
        val current = armed(context).toMutableMap()
        if (current.remove(reminderId) != null) writeArmed(context, current)
    }

    /** Replace only the server-owned portion, preserving device-local alarms. */
    fun replaceServerArmed(context: Context, schedules: List<AlarmSchedule>) {
        val local = armed(context).filterValues { it.isLocalRegular }
        writeArmed(context, local + schedules.associateBy { it.reminderId })
    }

    private fun writeArmed(context: Context, map: Map<String, AlarmSchedule>) {
        val root = JSONObject()
        map.forEach { (id, schedule) -> root.put(id, schedule.toJson()) }
        // commit(), not apply(): the caller is often a BroadcastReceiver whose
        // process can be killed the instant onReceive returns, and an apply()
        // still in flight would be lost.
        prefs(context).edit().putString(KEY_ARMED, root.toString()).commit()
    }

    // Fired ledger ---------------------------------------------------------

    /**
     * Records that this alarm rang, and reports whether it is the FIRST time.
     *
     * The dedupe that matters: the same alarm can arrive twice within seconds,
     * once from the local schedule and once from the server's backstop push,
     * and waking someone twice at 3 AM for the same thing is its own bug.
     */
    fun claimFired(
        context: Context,
        reminderId: String,
        triggerAtIso: String,
        nowMs: Long,
    ): Boolean {
        val ledger = firedLedger(context)
        val occurrence = occurrenceKey(reminderId, triggerAtIso)
        val previous = ledger[occurrence]
        if (previous != null && nowMs - previous < FIRED_LEDGER_TTL_MS) return false
        val next = ledger.filterValues { nowMs - it < FIRED_LEDGER_TTL_MS }.toMutableMap()
        next[occurrence] = nowMs
        // A reminder-only alias keeps backstops from older backend revisions
        // suppressible. Local alarm claims never consult this alias, so a snooze
        // with the same reminder id remains a distinct occurrence and can ring.
        next[reminderId] = nowMs
        writeFired(context, next)
        return true
    }

    // Device-local regular alarm ------------------------------------------

    fun regularAlarm(context: Context): RegularAlarm? {
        val raw = prefs(context).getString(KEY_REGULAR_ALARM, null) ?: return null
        return runCatching { RegularAlarm.fromJson(JSONObject(raw)) }.getOrNull()
    }

    fun putRegularAlarm(context: Context, alarm: RegularAlarm) {
        prefs(context).edit()
            .putString(KEY_REGULAR_ALARM, alarm.normalized().toJson().toString())
            .commit()
    }

    fun firedRecently(
        context: Context,
        reminderId: String,
        triggerAtIso: String,
        nowMs: Long,
    ): Boolean {
        val at = firedLedger(context)[occurrenceKey(reminderId, triggerAtIso)] ?: return false
        return nowMs - at < FIRED_LEDGER_TTL_MS
    }

    private fun occurrenceKey(reminderId: String, triggerAtIso: String): String {
        if (triggerAtIso.isBlank()) return reminderId
        val epochMs = runCatching { java.time.Instant.parse(triggerAtIso).toEpochMilli() }.getOrNull()
            ?: return "$reminderId@$triggerAtIso"
        return "$reminderId@$epochMs"
    }

    private fun firedLedger(context: Context): Map<String, Long> {
        val flutterPrefs = context.applicationContext.getSharedPreferences(
            FLUTTER_PREFS,
            Context.MODE_PRIVATE,
        )
        val raw = flutterPrefs.getString(FLUTTER_KEY_FIRED, null)
            ?: prefs(context).getString(KEY_FIRED, null)
            ?: return emptyMap()
        return try {
            val root = JSONObject(raw)
            root.keys().asSequence().associateWith { root.optLong(it, 0L) }
        } catch (_: Throwable) {
            emptyMap()
        }
    }

    private fun writeFired(context: Context, map: Map<String, Long>) {
        val root = JSONObject()
        map.forEach { (id, at) -> root.put(id, at) }
        // This is deliberately the preference file used by Flutter's
        // shared_preferences plugin. Firebase's background Flutter engine does
        // not run MainActivity.configureFlutterEngine, so the custom alarm
        // MethodChannel is unavailable there. Sharing this one durable ledger
        // lets the background handler suppress a backstop without Firebase,
        // network access, or an Activity-bound channel.
        context.applicationContext
            .getSharedPreferences(FLUTTER_PREFS, Context.MODE_PRIVATE)
            .edit()
            .putString(FLUTTER_KEY_FIRED, root.toString())
            .commit()
    }

    // Pending acks ---------------------------------------------------------

    /**
     * Queue an ack the app will send when it next runs.
     *
     * Dismiss and Snooze do not open the app, and at 3 AM there may be no
     * network at all, so the ack cannot be an HTTP call made here. The device
     * has already acted, it stopped ringing, it re-armed the snooze, and this
     * is only the server catching up. `nextTriggerAt` carries the exact moment
     * this device armed, so a flush hours later records the real snooze rather
     * than one counted from whenever the app happened to open.
     */
    fun queueAck(
        context: Context,
        reminderId: String,
        action: String,
        nextTriggerAtIso: String?,
    ) {
        val queue = pendingAcks(context)
        val entry = JSONObject()
            .put("reminder_id", reminderId)
            .put("action", action)
        if (!nextTriggerAtIso.isNullOrBlank()) entry.put("next_trigger_at", nextTriggerAtIso)
        // Last write wins per reminder: dismiss-after-snooze must not replay the
        // snooze, and an unbounded queue on a device that is offline for a week
        // would grow without limit.
        val merged = JSONArray()
        for (i in 0 until queue.length()) {
            val existing = queue.optJSONObject(i) ?: continue
            if (existing.optString("reminder_id") != reminderId) merged.put(existing)
        }
        merged.put(entry)
        prefs(context).edit().putString(KEY_PENDING_ACKS, merged.toString()).commit()
    }

    fun pendingAcks(context: Context): JSONArray {
        val raw = prefs(context).getString(KEY_PENDING_ACKS, null) ?: return JSONArray()
        return try {
            JSONArray(raw)
        } catch (_: Throwable) {
            JSONArray()
        }
    }

    fun clearPendingAcks(context: Context) {
        prefs(context).edit().remove(KEY_PENDING_ACKS).commit()
    }

    // Permission prompt rate limit ----------------------------------------

    /**
     * Claim the right to show the permission prompt, at most once per interval.
     *
     * Returns true for the caller that may prompt. Claim-then-act rather than
     * check-then-act so two alarms created in the same second cannot both pass.
     */
    fun claimPermissionPrompt(context: Context, nowMs: Long, intervalMs: Long): Boolean {
        val last = prefs(context).getLong(KEY_PROMPTED_AT, 0L)
        if (last != 0L && nowMs - last < intervalMs) return false
        prefs(context).edit().putLong(KEY_PROMPTED_AT, nowMs).commit()
        return true
    }

    /** Allow the prompt again immediately, e.g. after the permission is revoked. */
    fun resetPermissionPrompt(context: Context) {
        prefs(context).edit().remove(KEY_PROMPTED_AT).commit()
    }

    // Inexact (degraded) alarms -------------------------------------------

    fun markInexact(context: Context, reminderId: String, inexact: Boolean) {
        val current = inexactIds(context).toMutableSet()
        val changed = if (inexact) current.add(reminderId) else current.remove(reminderId)
        if (changed) {
            prefs(context).edit().putStringSet(KEY_INEXACT, current).commit()
        }
    }

    fun isInexact(context: Context, reminderId: String): Boolean =
        reminderId in inexactIds(context)

    fun inexactIds(context: Context): Set<String> =
        prefs(context).getStringSet(KEY_INEXACT, emptySet()) ?: emptySet()

    // Device-picked tone ---------------------------------------------------

    /**
     * The URI the user chose with the system ringtone picker, or null.
     *
     * Kept on the device and never synced. It is a content URI into this
     * phone's own storage: it would mean nothing on another device, and it can
     * name a file the user has no reason to have told a server about.
     */
    fun deviceToneUri(context: Context): String? =
        prefs(context).getString(KEY_DEVICE_TONE_URI, null)

    fun putDeviceToneUri(context: Context, uri: String?) {
        val editor = prefs(context).edit()
        if (uri.isNullOrBlank()) editor.remove(KEY_DEVICE_TONE_URI) else editor.putString(KEY_DEVICE_TONE_URI, uri)
        editor.commit()
    }

    // Global tone mirror ----------------------------------------------------

    fun defaultTone(context: Context): String =
        prefs(context).getString(KEY_DEFAULT_TONE, "").orEmpty()

    fun putDefaultTone(context: Context, tone: String) {
        prefs(context).edit().putString(KEY_DEFAULT_TONE, tone).commit()
    }

    // Ring anchor ----------------------------------------------------------

    /**
     * Record the instant the audio actually started, and what is playing.
     *
     * This is how [AlarmRippleView] stays in time with the sound without any
     * IPC. The service and the activity are separate components that start
     * moments apart, so the view cannot time ripples from its own creation; it
     * reads this anchor and derives the beat index from elapsed/period. Every
     * bundled clip loops on an exact multiple of its beat, so one anchor holds
     * for as long as the alarm rings.
     *
     * Written with commit() at the moment MediaPlayer.start() returns, so the
     * activity cannot read a half-written anchor even if it wins the race.
     */
    fun markRingStarted(context: Context, tone: String, atMs: Long) {
        prefs(context).edit()
            .putLong(KEY_RING_STARTED_AT, atMs)
            .putString(KEY_RING_TONE, tone)
            .commit()
    }

    fun clearRingAnchor(context: Context) {
        prefs(context).edit()
            .remove(KEY_RING_STARTED_AT)
            .remove(KEY_RING_TONE)
            .commit()
    }

    /** Epoch millis the current ring started, or 0 when nothing is ringing. */
    fun ringStartedAt(context: Context): Long =
        prefs(context).getLong(KEY_RING_STARTED_AT, 0L)

    /** The slug currently sounding, for picking a beat period. */
    fun ringTone(context: Context): String =
        prefs(context).getString(KEY_RING_TONE, "").orEmpty()
}
