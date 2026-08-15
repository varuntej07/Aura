package dev.varuntej.aura.alarm

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.util.Log

/**
 * Re-arms alarms after the OS throws them away.
 *
 * AlarmManager holds its schedule in memory. A reboot clears it completely, and
 * so does replacing the app's package on update. Nothing warns the user: they set
 * an alarm for 6 AM, their phone restarts overnight for a system update, and it
 * simply never rings. That is the same class of silent failure this whole feature
 * exists to close, so it is handled here rather than left to the next app open.
 *
 * Also listens for timezone changes, because an alarm is a wall-clock promise:
 * "wake me at 6" has to mean 6 AM where the sleeper actually is. See
 * [AlarmSchedule.resolveTriggerMillis] for what is re-anchored and what is
 * deliberately left alone.
 *
 * Re-arms from the LOCAL store only. This receiver runs before the user unlocks,
 * with no Flutter engine and no Firebase token, so there is no authenticated call
 * it could make. The store already holds everything needed; a full reconcile
 * against the server happens on the next app open.
 */
class AlarmBootReceiver : BroadcastReceiver() {

    override fun onReceive(context: Context, intent: Intent) {
        val action = intent.action ?: return
        when (action) {
            Intent.ACTION_BOOT_COMPLETED,
            Intent.ACTION_LOCKED_BOOT_COMPLETED,
            Intent.ACTION_MY_PACKAGE_REPLACED,
            ACTION_QUICKBOOT_POWERON,
            ACTION_HTC_QUICKBOOT_POWERON,
            -> {
                val count = AlarmScheduler.rearmAll(context)
                Log.i(TAG, "re-armed $count alarms after $action")
            }

            Intent.ACTION_TIMEZONE_CHANGED,
            Intent.ACTION_TIME_CHANGED,
            -> {
                // Every armed alarm is re-registered so its wall clock is
                // re-resolved against the new zone. Alarms that moved further
                // than the silent-shift limit keep their original instant and
                // are left for the app to confirm with the user.
                val count = AlarmScheduler.rearmAll(context)
                Log.i(TAG, "re-anchored $count alarms after $action")
            }
        }
    }

    companion object {
        private const val TAG = "AuraAlarm"

        // Some OEM ROMs (HTC, and several Chinese vendors that copied it) send
        // these instead of, or well before, ACTION_BOOT_COMPLETED on a fast
        // restart. Cheap to listen for; rearmAll is idempotent.
        const val ACTION_QUICKBOOT_POWERON = "android.intent.action.QUICKBOOT_POWERON"
        const val ACTION_HTC_QUICKBOOT_POWERON = "com.htc.intent.action.QUICKBOOT_POWERON"
    }
}
