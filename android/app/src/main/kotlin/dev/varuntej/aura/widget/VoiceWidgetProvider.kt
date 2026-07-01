package dev.varuntej.aura.widget

import android.app.PendingIntent
import android.appwidget.AppWidgetManager
import android.appwidget.AppWidgetProvider
import android.content.Context
import android.content.Intent
import android.widget.RemoteViews
import dev.varuntej.aura.MainActivity
import dev.varuntej.aura.R

/**
 * Home-screen widget that opens Aura straight into a live voice session: one tap,
 * mic on. Tapping anywhere on the widget launches [MainActivity] with the
 * [MainActivity.EXTRA_LAUNCH_ACTION] = [MainActivity.LAUNCH_ACTION_VOICE] extra.
 * The Flutter side reads it on startup (cold launch) or via onNewIntent (warm
 * launch) and starts the voice session. See VoiceLauncherBridge (Dart).
 *
 * The widget is created from inside the app (Settings -> "Add to home screen",
 * which calls AppWidgetManager.requestPinAppWidget); long-pressing the home
 * screen and picking Aura from the widget tray works too.
 */
class VoiceWidgetProvider : AppWidgetProvider() {

    override fun onUpdate(
        context: Context,
        appWidgetManager: AppWidgetManager,
        appWidgetIds: IntArray,
    ) {
        for (widgetId in appWidgetIds) {
            val views = RemoteViews(context.packageName, R.layout.voice_widget)
            views.setOnClickPendingIntent(
                R.id.voice_widget_root,
                buildVoiceLaunchIntent(context, widgetId),
            )
            appWidgetManager.updateAppWidget(widgetId, views)
        }
    }

    // The tap target: resume the single Flutter activity (singleTop) carrying the
    // "open with mic on" launch action. An explicit-component intent (deliberately
    // NOT ACTION_MAIN/CATEGORY_LAUNCHER, which the system dedupes against the task's
    // original launch intent and resumes without re-delivering extras) with
    // NEW_TASK + SINGLE_TOP brings the existing task to front and fires onNewIntent
    // with the extra when the app is already running, or onCreate when it's cold.
    private fun buildVoiceLaunchIntent(context: Context, widgetId: Int): PendingIntent {
        val intent = Intent(context, MainActivity::class.java).apply {
            putExtra(MainActivity.EXTRA_LAUNCH_ACTION, MainActivity.LAUNCH_ACTION_VOICE)
            flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP
        }
        // FLAG_IMMUTABLE is required on API 31+; a per-widget request code keeps
        // multiple placed widgets from clobbering each other's PendingIntent.
        val flags = PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        return PendingIntent.getActivity(context, widgetId, intent, flags)
    }
}
