package dev.varuntej.aura.alarm

import android.app.AlarmManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.util.Log

/**
 * Registers alarms with the OS.
 *
 * The one thing worth understanding here is why this exists at all rather than
 * the backend simply pushing a notification at 3 AM: it cannot. FCM delivery to
 * a doze'd phone is best-effort and deferred by design, and no priority flag
 * changes that. The only mechanism that reliably rings at a specific moment is a
 * schedule the OS itself holds, which is what [AlarmManager.setAlarmClock]
 * registers. It is exempt from doze, it survives the app being swiped away, and
 * it works with no network at all.
 *
 * What it does NOT survive: a device reboot, a package replace, and a user
 * force-stop. The first two are handled by [AlarmBootReceiver] re-arming from
 * the server. Force-stop is unrecoverable until the app is opened again, the
 * OS cancels the alarms and blocks the receivers, and the server's backstop
 * push is the only (weak) cover for it. That is a real hole, stated rather than
 * papered over.
 */
object AlarmScheduler {

    private const val TAG = "AuraAlarm"

    /**
     * Arm one alarm. Returns false when the OS refused, which the caller must
     * surface rather than swallow, a silently unarmed alarm IS the bug.
     */
    fun arm(context: Context, schedule: AlarmSchedule): Boolean {
        val manager = context.getSystemService(Context.ALARM_SERVICE) as? AlarmManager
            ?: return false
        val triggerAt = schedule.resolveTriggerMillis()
        if (triggerAt <= System.currentTimeMillis()) {
            // Already past. Arming it would fire immediately, which for an alarm
            // whose moment has gone is worse than nothing.
            Log.w(TAG, "skip arm, trigger in the past: ${schedule.reminderId}")
            AlarmStore.removeArmed(context, schedule.reminderId)
            return false
        }
        if (!AlarmPermissions.canScheduleExact(context)) {
            Log.w(TAG, "exact alarms denied, arming inexact: ${schedule.reminderId}")
            // Ask for the permission at the one moment the user demonstrably
            // wants it: they just asked Buddy for an alarm.
            AlarmPermissionPrompt.maybePrompt(context, schedule)
            return armInexact(context, manager, schedule, triggerAt)
        }

        return try {
            manager.setAlarmClock(
                // setAlarmClock, not setExactAndAllowWhileIdle: it is the only
                // API the OS treats as a real alarm clock. It ignores doze
                // entirely (rather than the once-per-9-minutes budget the "allow
                // while idle" variants get), and it puts the alarm icon in the
                // status bar so the user can see the promise was kept.
                AlarmManager.AlarmClockInfo(triggerAt, showIntent(context, schedule)),
                firePendingIntent(context, schedule),
            )
            AlarmStore.putArmed(context, schedule)
            AlarmStore.markInexact(context, schedule.reminderId, false)
            Log.i(TAG, "armed ${schedule.reminderId} at $triggerAt")
            true
        } catch (t: Throwable) {
            // SecurityException here means the permission was revoked between the
            // check above and this call. Loud, never silent: the whole feature is
            // a promise to make noise. Still worth an inexact alarm rather than
            // nothing at all.
            Log.e(TAG, "exact arm FAILED for ${schedule.reminderId}", t)
            AlarmPermissionPrompt.maybePrompt(context, schedule)
            armInexact(context, manager, schedule, triggerAt)
        }
    }

    /**
     * The honest fallback when exact alarms are refused.
     *
     * `setAndAllowWhileIdle` is NOT one of the APIs that throws without
     * `SCHEDULE_EXACT_ALARM` (only `setExact`, `setExactAndAllowWhileIdle` and
     * `setAlarmClock` are), so it still works. The cost is real: under Doze the
     * OS batches these and permits roughly one per app every nine minutes, so the
     * alarm can land several minutes late.
     *
     * Several minutes late is a genuine degradation and is still enormously
     * better than silence, which is what this path used to do. The alarm is
     * flagged inexact so the app can say "might be a few minutes off" instead of
     * promising a wake-up, and so telemetry never counts it as a real alarm.
     *
     * Returns false regardless: the caller's question is "is this a real alarm",
     * and the truthful answer here is no.
     */
    private fun armInexact(
        context: Context,
        manager: AlarmManager,
        schedule: AlarmSchedule,
        triggerAt: Long,
    ): Boolean {
        try {
            manager.setAndAllowWhileIdle(
                AlarmManager.RTC_WAKEUP,
                triggerAt,
                firePendingIntent(context, schedule),
            )
            Log.i(TAG, "armed INEXACT ${schedule.reminderId} at $triggerAt")
        } catch (t: Throwable) {
            Log.e(TAG, "inexact arm FAILED for ${schedule.reminderId}", t)
        }
        // Recorded either way, so a reconcile after the user grants the
        // permission upgrades it to a real alarm without another round trip.
        AlarmStore.putArmed(context, schedule)
        AlarmStore.markInexact(context, schedule.reminderId, true)
        return false
    }

    fun disarm(context: Context, reminderId: String) {
        val manager = context.getSystemService(Context.ALARM_SERVICE) as? AlarmManager
        existingFirePendingIntent(context, reminderId)?.let {
            manager?.cancel(it)
            it.cancel()
        }
        AlarmStore.removeArmed(context, reminderId)
        AlarmStore.markInexact(context, reminderId, false)
        Log.i(TAG, "disarmed $reminderId")
    }

    /**
     * Make the device's armed set match [schedules] exactly.
     *
     * Replace, not merge. The server's answer is complete, so an alarm that is
     * absent from it was cancelled somewhere else and must stop ringing here. A
     * merge would leave it armed forever with nothing able to clear it.
     */
    fun reconcile(context: Context, schedules: List<AlarmSchedule>): Int {
        val incoming = schedules.associateBy { it.reminderId }
        AlarmStore.armed(context)
            .filterValues { !it.isLocalRegular }
            .keys
            .filter { it !in incoming }
            .forEach { disarm(context, it) }

        var armedCount = 0
        // Rewrite the SERVER record first so a crash mid-loop cannot leave the
        // store claiming alarms that were never registered. Device-local alarms
        // are a separate authority and must survive an empty server response.
        AlarmStore.replaceServerArmed(context, schedules)
        schedules.forEach { if (arm(context, it)) armedCount++ }
        Log.i(TAG, "reconciled: ${schedules.size} known, $armedCount armed")
        return armedCount
    }

    /**
     * Re-register everything already known locally: after a reboot, after a
     * package replace, after a timezone change, and after the user grants the
     * exact-alarm permission.
     *
     * That last case is what makes returning from the Settings screen work.
     * Alarms refused earlier are still in the store (armed inexactly), so this
     * upgrades them to real alarms with no round trip to the server.
     */
    fun rearmAll(context: Context): Int {
        // Recompute the regular wake-up from its wall-clock definition. This is
        // what makes timezone changes and overnight reboots keep "7:30" rather
        // than a stale absolute instant. A pending snooze uses a separate id and
        // remains in the armed store.
        RegularAlarmCoordinator.refreshPrimary(context)
        val known = AlarmStore.armed(context).values.toList()
        var count = 0
        known.forEach { if (arm(context, it)) count++ }
        // The prompt is answered, so stop asking. Also re-allow the prompt if the
        // permission is gone again, so a later revoke is not silently swallowed
        // by the 24h rate limit set the last time it was refused.
        if (AlarmPermissions.canScheduleExact(context)) {
            AlarmPermissionPrompt.dismiss(context)
        } else {
            AlarmStore.resetPermissionPrompt(context)
        }
        Log.i(TAG, "rearmed $count of ${known.size}")
        return count
    }

    // PendingIntents -------------------------------------------------------

    private fun firePendingIntent(context: Context, schedule: AlarmSchedule): PendingIntent =
        PendingIntent.getBroadcast(
            context,
            requestCode(schedule.reminderId),
            AlarmReceiver.fireIntent(context, schedule),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )

    /**
     * The already-registered PendingIntent for this alarm, or null if none.
     *
     * PendingIntent identity ignores extras and matches on action, component and
     * request code, so rebuilding the shell here is enough to find the one the
     * arm path created. NO_CREATE means cancelling an alarm that was never armed
     * is a no-op rather than minting one purely to throw it away.
     */
    private fun existingFirePendingIntent(context: Context, reminderId: String): PendingIntent? =
        PendingIntent.getBroadcast(
            context,
            requestCode(reminderId),
            Intent(context, AlarmReceiver::class.java).setAction(AlarmReceiver.ACTION_FIRE),
            PendingIntent.FLAG_NO_CREATE or PendingIntent.FLAG_IMMUTABLE,
        )

    /** The full-screen UI the system shows if the user taps the status-bar alarm icon. */
    private fun showIntent(context: Context, schedule: AlarmSchedule): PendingIntent =
        PendingIntent.getActivity(
            context,
            requestCode(schedule.reminderId),
            AlarmActivity.launchIntent(context, schedule),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )

    /**
     * A stable per-alarm request code. PendingIntent identity ignores extras, so
     * without a distinct code every alarm would overwrite the previous one and a
     * user with two alarms would get one.
     */
    fun requestCode(reminderId: String): Int = reminderId.hashCode()
}
