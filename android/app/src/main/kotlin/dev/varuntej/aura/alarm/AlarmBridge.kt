package dev.varuntej.aura.alarm

import android.content.Context
import io.flutter.plugin.common.BinaryMessenger
import io.flutter.plugin.common.MethodChannel
import org.json.JSONArray
import org.json.JSONObject

/**
 * The Dart side of alarms.
 *
 * The division of labour is deliberate and worth stating: Dart owns the network
 * (it has a live Firebase token) and Kotlin owns the schedule (it is the only
 * half that exists when the alarm actually fires). So Dart fetches
 * GET /reminders/alarms and hands the result down here, and Kotlin hands back
 * the acks it took while Dart was not running.
 *
 * Nothing here is on the ringing path. If this channel never works, alarms still
 * ring off whatever is already armed.
 */
object AlarmBridge {

    const val CHANNEL = "dev.varuntej.aura/alarm"

    /**
     * Opens the system ringtone picker, set by MainActivity.
     *
     * Lives here as a hook rather than in this file's own code because picking a
     * ringtone needs an Activity result and this bridge only ever holds an
     * application context. Null whenever no Activity is attached, in which case
     * the call reports failure instead of crashing: the tone picker is a
     * settings screen, and nothing on the ringing path depends on it.
     */
    var deviceTonePicker: ((MethodChannel.Result) -> Unit)? = null

    fun register(messenger: BinaryMessenger, context: Context) {
        MethodChannel(messenger, CHANNEL).setMethodCallHandler { call, result ->
            val app = context.applicationContext
            when (call.method) {
                // Whether this device can actually keep the promise. Read BEFORE
                // Buddy confirms an alarm, so it can say "I'll nudge you" instead
                // of "I'll wake you" when the OS has not granted the permission.
                "capabilities" -> result.success(
                    mapOf(
                        "can_schedule_exact" to AlarmPermissions.canScheduleExact(app),
                        "can_use_full_screen_intent" to AlarmPermissions.canUseFullScreenIntent(app),
                        "can_ring" to AlarmPermissions.canRing(app),
                        // Alarms currently armed inexactly because the permission
                        // was refused. Non-empty means the app is showing someone
                        // an alarm that will fire late, which the UI must say.
                        "degraded_alarm_count" to AlarmStore.inexactIds(app).size,
                    ),
                )

                // Re-register everything after the user returns from the Settings
                // screen. Upgrades any alarm armed inexactly into a real one.
                "rearmAll" -> result.success(AlarmScheduler.rearmAll(app))

                "requestExactAlarmAccess" ->
                    result.success(AlarmPermissions.requestExactAlarmAccess(app))

                "requestFullScreenIntentAccess" ->
                    result.success(AlarmPermissions.requestFullScreenIntentAccess(app))

                // Replace the armed set with the server's complete answer. See
                // AlarmScheduler.reconcile for why this replaces rather than merges.
                "reconcile" -> {
                    val schedules = parseSchedules(call.argument<String>("alarms"))
                    if (schedules == null) {
                        result.error("invalid_args", "alarms must be a JSON array", null)
                    } else {
                        result.success(AlarmScheduler.reconcile(app, schedules))
                    }
                }

                // Arm one alarm from the create-time FCM control push, without
                // waiting for the next full reconcile.
                "arm" -> {
                    val schedule = parseSchedule(call.argument<String>("alarm"))
                    if (schedule == null) {
                        result.error("invalid_args", "alarm must be a JSON object", null)
                    } else {
                        result.success(AlarmScheduler.arm(app, schedule))
                    }
                }

                "disarm" -> {
                    val id = call.argument<String>("reminder_id")
                    if (id.isNullOrBlank()) {
                        result.error("invalid_args", "reminder_id is required", null)
                    } else {
                        AlarmScheduler.disarm(app, id)
                        result.success(true)
                    }
                }

                // Silence an alarm ringing on THIS device because the user
                // already dealt with it on another one.
                "stopRinging" -> {
                    AlarmService.stop(app)
                    result.success(true)
                }

                // True when this alarm already rang here recently, so the
                // server's backstop push can be suppressed instead of waking the
                // user a second time for the thing that worked.
                "firedRecently" -> {
                    val id = call.argument<String>("reminder_id")
                    val triggerAt = call.argument<String>("trigger_at").orEmpty()
                    result.success(
                        !id.isNullOrBlank() &&
                            AlarmStore.firedRecently(
                                app,
                                id,
                                triggerAt,
                                System.currentTimeMillis(),
                            ),
                    )
                }

                // Acks taken while Dart was not running (Dismiss and Snooze never
                // open the app). Dart posts them and calls clearPendingAcks only
                // once the server has accepted them.
                "pendingAcks" -> result.success(AlarmStore.pendingAcks(app).toString())

                "clearPendingAcks" -> {
                    AlarmStore.clearPendingAcks(app)
                    result.success(true)
                }

                // The sound the user picked from their own device, if any. The
                // URI never leaves the phone: it is a path into their storage
                // and would mean nothing anywhere else.
                "deviceTone" -> result.success(
                    mapOf(
                        "uri" to AlarmStore.deviceToneUri(app).orEmpty(),
                        "title" to AlarmTones.deviceToneTitle(app),
                    ),
                )

                "pickDeviceTone" -> {
                    val picker = deviceTonePicker
                    if (picker == null) {
                        result.error("no_activity", "No activity is attached", null)
                    } else {
                        picker(result)
                    }
                }

                // Firestore is the source of truth. This native copy is the
                // offline mirror available before a Flutter engine exists.
                "setDefaultTone" -> {
                    val tone = call.argument<String>("tone")?.trim()?.lowercase().orEmpty()
                    if (!AlarmTones.isSelectable(tone)) {
                        result.error("invalid_args", "unknown alarm tone", null)
                    } else {
                        AlarmStore.putDefaultTone(app, tone)
                        result.success(true)
                    }
                }

                else -> result.notImplemented()
            }
        }
    }

    private fun parseSchedules(raw: String?): List<AlarmSchedule>? {
        if (raw == null) return null
        return try {
            val array = JSONArray(raw)
            (0 until array.length())
                .mapNotNull { array.optJSONObject(it) }
                .mapNotNull { AlarmSchedule.fromJson(it) }
        } catch (_: Throwable) {
            null
        }
    }

    private fun parseSchedule(raw: String?): AlarmSchedule? {
        if (raw == null) return null
        return try {
            AlarmSchedule.fromJson(JSONObject(raw))
        } catch (_: Throwable) {
            null
        }
    }
}
