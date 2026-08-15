package dev.varuntej.aura.alarm

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.provider.Settings
import android.util.Log
import dev.varuntej.aura.R

/**
 * Asks for the one permission that makes alarms real.
 *
 * From Android 14, `SCHEDULE_EXACT_ALARM` is not pre-granted, and there is no
 * runtime dialog for it: the only way to get it is a Settings screen. So this
 * posts a notification whose tap deep-links straight to Aura's own "Alarms &
 * reminders" toggle. One tap from the shade to the switch.
 *
 * WHY A NOTIFICATION AND NOT AN IN-APP DIALOG
 * -------------------------------------------
 * The moment worth asking at is the moment the user asks Buddy for an alarm, and
 * that is almost never a moment the app is in the foreground: an alarm is set
 * hours before it fires, usually by voice or from the keyboard, and the schedule
 * arrives as a silent push to a backgrounded app. A dialog would be seen at the
 * next app open, which may be after the alarm was supposed to ring.
 *
 * Deliberately NOT on the alarm channel. This is a setup prompt, not an alarm: it
 * must not inherit alarm volume, DND bypass, or a full-screen intent. Waking
 * someone at 3 AM to tell them their 3 AM alarm will not work is absurd.
 */
object AlarmPermissionPrompt {

    private const val TAG = "AuraAlarm"

    const val CHANNEL_ID = "aura_alarm_setup"
    private const val CHANNEL_NAME = "Alarm setup"
    private const val NOTIFICATION_ID = 90211

    /**
     * Ask at most once a day. Someone who sets three alarms in an evening has one
     * problem, not three, and a prompt per alarm would read as a malfunction.
     */
    private const val PROMPT_INTERVAL_MS = 24L * 60L * 60L * 1000L

    /**
     * Prompt for exact-alarm access, unless we already did today.
     *
     * Returns true if a prompt was posted. Never throws: this runs from the arm
     * path, and failing to ask must not also break the (degraded) alarm that was
     * just scheduled.
     */
    fun maybePrompt(context: Context, schedule: AlarmSchedule): Boolean {
        if (AlarmPermissions.canScheduleExact(context)) return false
        val now = System.currentTimeMillis()
        if (!AlarmStore.claimPermissionPrompt(context, now, PROMPT_INTERVAL_MS)) {
            Log.i(TAG, "permission prompt suppressed, already asked recently")
            return false
        }

        return try {
            createChannel(context)
            val manager = context.getSystemService(Context.NOTIFICATION_SERVICE)
                as? NotificationManager ?: return false
            manager.notify(NOTIFICATION_ID, build(context, schedule))
            Log.i(TAG, "posted exact-alarm permission prompt")
            true
        } catch (t: Throwable) {
            Log.e(TAG, "could not post permission prompt", t)
            false
        }
    }

    /** Clear the prompt once the permission has actually been granted. */
    fun dismiss(context: Context) {
        try {
            (context.getSystemService(Context.NOTIFICATION_SERVICE) as? NotificationManager)
                ?.cancel(NOTIFICATION_ID)
        } catch (_: Throwable) {
            // A stale prompt is untidy, not harmful.
        }
    }

    private fun createChannel(context: Context) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
        val manager = context.getSystemService(Context.NOTIFICATION_SERVICE)
            as? NotificationManager ?: return
        manager.createNotificationChannel(
            NotificationChannel(
                CHANNEL_ID,
                CHANNEL_NAME,
                // DEFAULT, not HIGH: important enough to see in the shade,
                // not important enough to interrupt.
                NotificationManager.IMPORTANCE_DEFAULT,
            ).apply {
                description = "Asks for the permission Buddy needs to wake you up."
                setShowBadge(false)
            },
        )
    }

    private fun build(context: Context, schedule: AlarmSchedule): Notification {
        val builder = Notification.Builder(context, CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_notification)
            .setContentTitle("Buddy can't wake you yet")
            .setContentText("Tap to let Buddy set alarms. Takes one tap.")
            .setStyle(
                Notification.BigTextStyle().bigText(
                    "Android needs your OK before Buddy can ring an alarm. " +
                        "Without it, \"${schedule.message.take(60)}\" will only be a " +
                        "silent reminder.",
                ),
            )
            .setContentIntent(settingsIntent(context))
            .setAutoCancel(true)
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) {
            @Suppress("DEPRECATION")
            builder.setPriority(Notification.PRIORITY_DEFAULT)
        }
        return builder.build()
    }

    /**
     * Deep link to Aura's own "Alarms & reminders" entry, not the generic Settings
     * root. The package URI is what makes it land on the toggle rather than a list
     * the user then has to hunt through half-asleep.
     */
    private fun settingsIntent(context: Context): PendingIntent {
        val intent = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            Intent(Settings.ACTION_REQUEST_SCHEDULE_EXACT_ALARM)
                .setData(Uri.parse("package:${context.packageName}"))
        } else {
            Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS)
                .setData(Uri.parse("package:${context.packageName}"))
        }
        return PendingIntent.getActivity(
            context,
            0,
            intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
    }
}
