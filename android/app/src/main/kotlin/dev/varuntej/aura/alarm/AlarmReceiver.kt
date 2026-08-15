package dev.varuntej.aura.alarm

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.os.Build
import android.util.Log

/**
 * The moment the alarm is due.
 *
 * Runs in whatever process the OS has, which at 3 AM is frequently one started
 * for this broadcast alone: no Flutter engine, no Firebase, no network. Every
 * decision here therefore reads only [AlarmStore] and hands off to a foreground
 * service within the few seconds a receiver is allowed to live.
 */
class AlarmReceiver : BroadcastReceiver() {

    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != ACTION_FIRE) return
        val schedule = scheduleFrom(context, intent) ?: run {
            Log.e(TAG, "fire intent carried no usable schedule")
            return
        }

        // Both the local alarm and the server's backstop push can land within
        // seconds of each other for the same alarm. Whichever arrives first
        // claims the ring; the second is dropped. Waking someone twice at 3 AM
        // for one alarm is its own bug.
        if (!AlarmStore.claimFired(
                context,
                schedule.reminderId,
                schedule.triggerAtIso,
                System.currentTimeMillis(),
            )
        ) {
            Log.i(TAG, "already ringing/rang, dropping duplicate: ${schedule.reminderId}")
            return
        }

        // The alarm has fired, so it is no longer armed. Cleared here rather than
        // when the user dismisses it, because the user may never dismiss it, and
        // a stale entry would be re-armed by the next reconcile.
        AlarmStore.removeArmed(context, schedule.reminderId)

        // A regular alarm schedules tomorrow/its next selected weekday from the
        // native definition immediately. This happens before any UI or Flutter
        // engine is involved, so ignoring/dismissing the ring cannot lose the
        // next occurrence. Snooze has a separate id and cannot overwrite it.
        if (schedule.isLocalRegular) {
            RegularAlarmCoordinator.scheduleNext(
                context,
                afterMillis = System.currentTimeMillis() + 1_000L,
            )
        }

        val serviceIntent = AlarmService.startIntent(context, schedule)
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                context.startForegroundService(serviceIntent)
            } else {
                context.startService(serviceIntent)
            }
        } catch (t: Throwable) {
            // A foreground-service start can be refused on some OEM builds. The
            // alarm must still make itself known, so fall back to launching the
            // full-screen activity directly rather than failing silently.
            Log.e(TAG, "foreground service start refused, falling back to activity", t)
            try {
                context.startActivity(
                    AlarmActivity.launchIntent(context, schedule)
                        .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
                )
            } catch (inner: Throwable) {
                Log.e(TAG, "alarm could not be surfaced at all: ${schedule.reminderId}", inner)
            }
        }
    }

    companion object {
        private const val TAG = "AuraAlarm"
        const val ACTION_FIRE = "dev.varuntej.aura.alarm.FIRE"
        const val EXTRA_SCHEDULE = "schedule_json"

        fun fireIntent(context: Context, schedule: AlarmSchedule): Intent =
            Intent(context, AlarmReceiver::class.java)
                .setAction(ACTION_FIRE)
                .putExtra(EXTRA_SCHEDULE, schedule.toJson().toString())

        /**
         * Read the schedule out of a fire intent, preferring the local store.
         *
         * The intent's copy was written when the alarm was armed and can be
         * stale (a snooze re-armed it, the message changed). The store is the
         * current record; the intent is the fallback for the case where the
         * store was cleared out from under us.
         */
        fun scheduleFrom(context: Context, intent: Intent): AlarmSchedule? {
            val fromIntent = try {
                intent.getStringExtra(EXTRA_SCHEDULE)
                    ?.let { AlarmSchedule.fromJson(org.json.JSONObject(it)) }
            } catch (_: Throwable) {
                null
            }
            val id = fromIntent?.reminderId ?: return null
            return AlarmStore.armed(context)[id] ?: fromIntent
        }
    }
}
