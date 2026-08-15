package dev.varuntej.aura.alarm

import android.app.AlarmManager
import android.app.NotificationManager
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.provider.Settings

/**
 * Whether this device will actually let Buddy wake someone up.
 *
 * This is the difference between the alarm tier working and reproducing the bug
 * it was built to fix. Android 14 denies BOTH of the permissions below by
 * default to any app that is not categorised as an alarm clock or a calendar,
 * and denies them SILENTLY: `setAlarmClock` throws or degrades, a full-screen
 * intent quietly becomes an ordinary notification, and nothing tells the user
 * that the 3 AM alarm they just set is never going to ring.
 *
 * So the app asks these questions BEFORE Buddy promises anything, and Buddy's
 * confirmation is conditioned on the answer. An alarm Buddy cannot deliver has
 * to be described as the nudge it really is.
 *
 * Deliberately NOT using USE_EXACT_ALARM. It is granted automatically, which is
 * tempting, but Play policy restricts it to apps whose core function is an alarm
 * clock or calendar. Aura is a companion app, so shipping it invites a policy
 * takedown of the whole listing. SCHEDULE_EXACT_ALARM plus a real request flow is
 * the honest path.
 */
object AlarmPermissions {

    /**
     * Can the OS be asked for an exact, doze-exempt alarm?
     *
     * Below Android 12 exact alarms need no permission at all. From 12 they do,
     * from 14 it is denied by default, and it can be revoked at any time by the
     * user or by the system reclaiming it from an unused app, so this is
     * re-checked on every arm, never cached.
     */
    fun canScheduleExact(context: Context): Boolean {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.S) return true
        val manager = context.getSystemService(Context.ALARM_SERVICE) as? AlarmManager
            ?: return false
        return manager.canScheduleExactAlarms()
    }

    /**
     * Can a notification take over the screen on a locked device?
     *
     * Losing this is a downgrade, not a failure: without it the alarm still
     * posts a heads-up notification and the foreground service still plays at
     * alarm volume and vibrates, which wakes people. The full-screen Buddy UI is
     * the upgrade on top.
     */
    fun canUseFullScreenIntent(context: Context): Boolean {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.UPSIDE_DOWN_CAKE) return true
        val manager = context.getSystemService(Context.NOTIFICATION_SERVICE) as? NotificationManager
            ?: return false
        return manager.canUseFullScreenIntent()
    }

    /** True when this device can ring properly, with or without the full-screen UI. */
    fun canRing(context: Context): Boolean = canScheduleExact(context)

    /**
     * Open the system page where exact alarms are granted.
     *
     * There is no runtime dialog for this one, it is a Settings screen only, so
     * the app has to send the user there and re-check when they come back.
     */
    fun requestExactAlarmAccess(context: Context): Boolean {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.S) return false
        return launch(
            context,
            Intent(Settings.ACTION_REQUEST_SCHEDULE_EXACT_ALARM)
                .setData(Uri.parse("package:${context.packageName}")),
        )
    }

    fun requestFullScreenIntentAccess(context: Context): Boolean {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.UPSIDE_DOWN_CAKE) return false
        return launch(
            context,
            Intent(Settings.ACTION_MANAGE_APP_USE_FULL_SCREEN_INTENT)
                .setData(Uri.parse("package:${context.packageName}")),
        )
    }

    private fun launch(context: Context, intent: Intent): Boolean = try {
        context.startActivity(intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
        true
    } catch (_: Throwable) {
        // Some OEM builds ship without the settings activity these intents name.
        // Falling back to the app's own settings page still gets the user to a
        // screen where the toggle exists.
        try {
            context.startActivity(
                Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS)
                    .setData(Uri.parse("package:${context.packageName}"))
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
            )
            true
        } catch (_: Throwable) {
            false
        }
    }
}
