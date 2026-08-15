package dev.varuntej.aura.alarm

import org.json.JSONObject
import java.time.Instant
import java.time.LocalDateTime
import java.time.ZoneId
import java.time.format.DateTimeParseException

/**
 * One armed alarm, exactly as the server describes it.
 *
 * Carries BOTH an absolute instant (`triggerAtIso`) and the wall clock the user
 * actually asked for (`localTimeIso` + `timezone`), because those are different
 * promises and an alarm is the second kind. "Wake me at 6" means 6 AM where the
 * sleeper is; if they fall asleep in Delhi and wake in London, the instant
 * computed at creation is 1:30 AM local and useless. A plain reminder is the
 * opposite: "the meeting starts at 14:00 UTC" must not move.
 *
 * See [resolveTriggerMillis] for how the two are reconciled.
 */
data class AlarmSchedule(
    val reminderId: String,
    val message: String,
    val triggerAtIso: String,
    val localTimeIso: String,
    val timezone: String,
    val snoozeCount: Int,

    /**
     * Which sound to ring with, already resolved by the server from the
     * per-alarm override and the user's default. One concrete slug; this side
     * owns no precedence rule. See [AlarmTones].
     */
    val tone: String = "",

    /**
     * Absolute path to a pre-rendered clip of Buddy reading this reminder, or
     * blank. Dart fetches and caches it at arm time, because at ring time there
     * is no Flutter engine and possibly no network. Blank is normal and simply
     * means the tone keeps looping without ever being interrupted to speak.
     */
    val voiceClipPath: String = "",

    /** Server-distributed reminder or the Settings-owned local wake-up alarm. */
    val source: String = SOURCE_SERVER,

    /** Per-occurrence vibration choice. Old/server schedules default to on. */
    val vibrate: Boolean = true,

    /** True only for the separate nine-minute local snooze occurrence. */
    val isSnooze: Boolean = false,
) {

    val isLocalRegular: Boolean get() = source == SOURCE_LOCAL_REGULAR

    /**
     * When this alarm should ring on THIS device, right now.
     *
     * Prefers the wall clock re-resolved against the device's current zone, so a
     * traveller is woken at the hour they asked for. Falls back to the absolute
     * instant when the wall clock is missing or unparseable, an alarm at
     * slightly the wrong time still beats no alarm.
     *
     * A large jump is deliberately NOT applied silently: see [wallClockShiftMs].
     */
    fun resolveTriggerMillis(now: Long = System.currentTimeMillis()): Long {
        val absolute = parseInstant(triggerAtIso) ?: return now
        val wall = parseWallClock() ?: return absolute
        val shift = wall - absolute
        // Beyond a few hours this is a timezone move, not DST or a rounding
        // difference, and re-anchoring a 3 AM alarm across it can only be a
        // guess. Keep the original instant and let the app confirm with the user
        // rather than silently moving an alarm they are relying on.
        return if (kotlin.math.abs(shift) > MAX_SILENT_SHIFT_MS) absolute else wall
    }

    /** How far the wall clock reading differs from the stored instant, in ms. */
    fun wallClockShiftMs(): Long {
        val absolute = parseInstant(triggerAtIso) ?: return 0L
        val wall = parseWallClock() ?: return 0L
        return wall - absolute
    }

    private fun parseWallClock(): Long? {
        if (localTimeIso.isBlank()) return null
        return try {
            LocalDateTime.parse(localTimeIso)
                .atZone(ZoneId.systemDefault())
                .toInstant()
                .toEpochMilli()
        } catch (_: DateTimeParseException) {
            null
        } catch (_: Throwable) {
            null
        }
    }

    fun toJson(): JSONObject = JSONObject()
        .put("reminder_id", reminderId)
        .put("message", message)
        .put("trigger_at", triggerAtIso)
        .put("local_time", localTimeIso)
        .put("timezone", timezone)
        .put("snooze_count", snoozeCount)
        .put("tone", tone)
        .put("voice_clip_path", voiceClipPath)
        .put("source", source)
        .put("vibrate", vibrate)
        .put("is_snooze", isSnooze)

    companion object {
        /** Three hours: wider than any DST step, narrower than a real flight. */
        const val MAX_SILENT_SHIFT_MS = 3L * 60L * 60L * 1000L
        const val SOURCE_SERVER = "server"
        const val SOURCE_LOCAL_REGULAR = "local_regular"

        fun fromJson(json: JSONObject): AlarmSchedule? {
            val id = json.optString("reminder_id").orEmpty()
            if (id.isBlank()) return null
            return AlarmSchedule(
                reminderId = id,
                message = json.optString("message").orEmpty(),
                triggerAtIso = json.optString("trigger_at").orEmpty(),
                localTimeIso = json.optString("local_time").orEmpty(),
                timezone = json.optString("timezone").orEmpty(),
                snoozeCount = json.optInt("snooze_count", 0),
                // Both default to blank rather than being required. Schedules
                // written by the previous build are already sitting in
                // SharedPreferences with neither key, and an upgrade that made
                // them unparseable would disarm every alarm on the device.
                tone = json.optString("tone").orEmpty(),
                voiceClipPath = json.optString("voice_clip_path").orEmpty(),
                source = json.optString("source", SOURCE_SERVER),
                vibrate = json.optBoolean("vibrate", true),
                isSnooze = json.optBoolean("is_snooze", false),
            )
        }

        fun parseInstant(iso: String): Long? {
            if (iso.isBlank()) return null
            return try {
                Instant.parse(iso.replace("+00:00", "Z")).toEpochMilli()
            } catch (_: Throwable) {
                try {
                    java.time.OffsetDateTime.parse(iso).toInstant().toEpochMilli()
                } catch (_: Throwable) {
                    null
                }
            }
        }
    }
}
