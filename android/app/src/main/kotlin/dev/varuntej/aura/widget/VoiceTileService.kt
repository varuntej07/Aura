package dev.varuntej.aura.widget

import android.app.PendingIntent
import android.content.Intent
import android.os.Build
import android.service.quicksettings.TileService
import dev.varuntej.aura.MainActivity

/**
 * Quick Settings tile that drops the user straight into a Buddy voice session from the
 * pull-down shade, anywhere in the OS. Reuses the exact "open with mic on" launch
 * intent the home-screen widget uses ([MainActivity.EXTRA_LAUNCH_ACTION] =
 * [MainActivity.LAUNCH_ACTION_VOICE]); the Flutter side starts voice on receipt. No
 * Dart change needed.
 */
class VoiceTileService : TileService() {

    override fun onClick() {
        super.onClick()
        val intent = Intent(this, MainActivity::class.java).apply {
            putExtra(MainActivity.EXTRA_LAUNCH_ACTION, MainActivity.LAUNCH_ACTION_VOICE)
            flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
            // Android 14+: the Intent overload throws; a PendingIntent is required.
            val flags = PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
            startActivityAndCollapse(PendingIntent.getActivity(this, 0, intent, flags))
        } else {
            @Suppress("DEPRECATION")
            startActivityAndCollapse(intent)
        }
    }
}
