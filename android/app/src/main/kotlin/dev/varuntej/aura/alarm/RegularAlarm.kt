package dev.varuntej.aura.alarm

import android.content.Context
import org.json.JSONArray
import org.json.JSONObject
import java.time.DayOfWeek
import java.time.Instant
import java.time.ZoneId
import java.time.ZonedDateTime

/**
 * The one device-local wake-up alarm configured from Settings.
 *
 * This is deliberately a definition, not a queue of seven future Firestore
 * rows. Android owns the next occurrence and derives another one whenever the
 * current occurrence fires. That keeps the everyday alarm offline-first and
 * independent of the backend scheduler.
 */
data class RegularAlarm(
    val enabled: Boolean = false,
    val hour: Int = 7,
    val minute: Int = 30,
    /** ISO weekday numbers: Monday=1 ... Sunday=7. */
    val weekdays: Set<Int> = ALL_WEEKDAYS,
    val tone: String = "",
    val vibrate: Boolean = true,
) {
    fun normalized(): RegularAlarm {
        val safeDays = weekdays.filter { it in 1..7 }.toSet().ifEmpty { ALL_WEEKDAYS }
        val safeTone = when {
            tone == AlarmTones.BUDDY -> AlarmTones.BED_SLUG
            AlarmTones.isSelectable(tone) -> tone
            else -> ""
        }
        return copy(
            hour = hour.coerceIn(0, 23),
            minute = minute.coerceIn(0, 59),
            weekdays = safeDays,
            tone = safeTone,
        )
    }

    /** The first selected wall-clock occurrence strictly after [after]. */
    fun nextOccurrence(after: ZonedDateTime = ZonedDateTime.now()): ZonedDateTime? {
        if (!enabled) return null
        val value = normalized()
        for (dayOffset in 0..7) {
            val date = after.toLocalDate().plusDays(dayOffset.toLong())
            if (date.dayOfWeek.value !in value.weekdays) continue
            val candidate = date.atTime(value.hour, value.minute).atZone(after.zone)
            if (candidate.isAfter(after)) return candidate
        }
        return null
    }

    fun toJson(): JSONObject = JSONObject()
        .put("enabled", enabled)
        .put("hour", hour)
        .put("minute", minute)
        .put("weekdays", JSONArray(weekdays.sorted()))
        .put("tone", tone)
        .put("vibrate", vibrate)

    fun toFlutterMap(nextTriggerAt: String? = null): Map<String, Any?> = mapOf(
        "enabled" to enabled,
        "hour" to hour,
        "minute" to minute,
        "weekdays" to weekdays.sorted(),
        "tone" to tone,
        "vibrate" to vibrate,
        "next_trigger_at" to nextTriggerAt,
    )

    companion object {
        val ALL_WEEKDAYS: Set<Int> = (DayOfWeek.MONDAY.value..DayOfWeek.SUNDAY.value).toSet()

        fun fromJson(json: JSONObject): RegularAlarm {
            val days = mutableSetOf<Int>()
            val rawDays = json.optJSONArray("weekdays")
            if (rawDays != null) {
                for (index in 0 until rawDays.length()) {
                    val day = rawDays.optInt(index, 0)
                    if (day in 1..7) days += day
                }
            }
            return RegularAlarm(
                enabled = json.optBoolean("enabled", false),
                hour = json.optInt("hour", 7),
                minute = json.optInt("minute", 30),
                weekdays = days.ifEmpty { ALL_WEEKDAYS },
                tone = json.optString("tone").orEmpty(),
                vibrate = json.optBoolean("vibrate", true),
            ).normalized()
        }
    }
}

/** Owns the lifecycle of the device-local regular alarm. */
object RegularAlarmCoordinator {
    const val PRIMARY_ID = "local:regular-wake-up"
    const val SNOOZE_ID = "local:regular-wake-up:snooze"

    fun snapshot(context: Context): Map<String, Any?> {
        val alarm = AlarmStore.regularAlarm(context) ?: RegularAlarm(
            enabled = true,
            tone = localDefaultTone(context),
        )
        val next = AlarmStore.armed(context)[PRIMARY_ID]?.triggerAtIso
        return alarm.toFlutterMap(next)
    }

    fun save(context: Context, alarm: RegularAlarm): Map<String, Any?> {
        val normalized = alarm.normalized()
        AlarmStore.putRegularAlarm(context, normalized)
        AlarmScheduler.disarm(context, PRIMARY_ID)
        AlarmScheduler.disarm(context, SNOOZE_ID)
        val next = if (normalized.enabled) scheduleNext(context) else null
        return normalized.toFlutterMap(next?.triggerAtIso)
    }

    /**
     * Recompute the primary occurrence in the current device timezone.
     *
     * Used at save time, immediately after an occurrence fires, and after a
     * reboot/timezone change. A snooze has its own id and therefore cannot
     * overwrite tomorrow's regular occurrence.
     */
    fun scheduleNext(
        context: Context,
        afterMillis: Long = System.currentTimeMillis(),
    ): AlarmSchedule? {
        val alarm = AlarmStore.regularAlarm(context)?.normalized() ?: return null
        if (!alarm.enabled) return null
        val zone = ZoneId.systemDefault()
        val after = Instant.ofEpochMilli(afterMillis).atZone(zone)
        val next = alarm.nextOccurrence(after) ?: return null
        val schedule = AlarmSchedule(
            reminderId = PRIMARY_ID,
            message = "Time to wake up.",
            triggerAtIso = next.toInstant().toString(),
            localTimeIso = next.toLocalDateTime().toString(),
            timezone = zone.id,
            snoozeCount = 0,
            tone = alarm.tone,
            source = AlarmSchedule.SOURCE_LOCAL_REGULAR,
            vibrate = alarm.vibrate,
        )
        AlarmScheduler.arm(context, schedule)
        return schedule
    }

    fun snooze(context: Context, current: AlarmSchedule, nextMillis: Long): AlarmSchedule {
        val snooze = current.copy(
            reminderId = SNOOZE_ID,
            triggerAtIso = Instant.ofEpochMilli(nextMillis).toString(),
            localTimeIso = "",
            timezone = ZoneId.systemDefault().id,
            snoozeCount = current.snoozeCount + 1,
            source = AlarmSchedule.SOURCE_LOCAL_REGULAR,
            isSnooze = true,
        )
        AlarmScheduler.arm(context, snooze)
        return snooze
    }

    /** Rebuild only the primary occurrence; a pending snooze remains intact. */
    fun refreshPrimary(context: Context) {
        AlarmScheduler.disarm(context, PRIMARY_ID)
        scheduleNext(context)
    }

    private fun localDefaultTone(context: Context): String {
        val stored = AlarmStore.defaultTone(context)
        return when {
            stored == AlarmTones.BUDDY -> AlarmTones.BED_SLUG
            AlarmTones.isSelectable(stored) -> stored
            else -> ""
        }
    }
}
