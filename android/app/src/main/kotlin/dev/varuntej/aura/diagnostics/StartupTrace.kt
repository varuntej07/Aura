package dev.varuntej.aura.diagnostics

import android.content.Context
import android.content.SharedPreferences
import java.util.UUID

/**
 * A boot-stage breadcrumb that survives process death.
 *
 * Written as a single string at each startup milestone and cleared once the
 * first frame renders. If the NEXT launch finds a stage other than
 * [STAGE_FIRST_FRAME], that stage names the exact step that killed the previous
 * process — which is the one thing no crash reporter can tell us when the
 * process dies before the reporter is armed.
 *
 * Three deliberate choices, each one because the obvious alternative is itself a
 * suspect in the failure we are chasing:
 *
 *  * Plain [SharedPreferences], NOT `EncryptedSharedPreferences`. The encrypted
 *    variant does an AndroidKeyStore round trip, which is a real startup failure
 *    candidate on OEM builds. The breadcrumb must not be able to crash the thing
 *    it is measuring. Nothing written here is sensitive.
 *  * The Android framework API, NOT the `shared_preferences` Flutter plugin.
 *    `shared_preferences_android` is pinned below 2.4.26 in pubspec.yaml because
 *    newer versions "silently drop their FlutterPlugin entry class from the
 *    RELEASE-variant Kotlin compile", so the plugin is on the suspect list and
 *    cannot be the instrument.
 *  * `commit()`, NOT `apply()`. `apply()` is asynchronous and would lose the
 *    marker in exactly the case we care about: the process dying moments later.
 */
object StartupTrace {

    private const val PREFS_NAME = "aura_startup_trace"

    private const val KEY_CURRENT_STAGE = "current_stage"
    private const val KEY_LAUNCH_COUNT = "launch_count"
    private const val KEY_INSTALL_ID = "install_id"
    private const val KEY_CONSECUTIVE_FAILURES = "consecutive_failed_launches"

    // Native stages, stamped from MainActivity.
    const val STAGE_NATIVE_ON_CREATE = "native_oncreate"
    const val STAGE_ENGINE_CONFIGURED = "engine_configured"

    // Dart stages, stamped over the diagnostics MethodChannel. The names are the
    // contract with StartupDiagnosticsService on the Dart side.
    const val STAGE_FIRST_FRAME = "first_frame"

    private fun prefs(context: Context): SharedPreferences =
        context.applicationContext.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)

    /**
     * Records that startup reached [stage]. Never throws: a diagnostics failure
     * must never be the reason the app dies.
     */
    fun stamp(context: Context, stage: String) {
        try {
            prefs(context).edit().putString(KEY_CURRENT_STAGE, stage).commit()
        } catch (_: Throwable) {
            // Intentionally swallowed. See the class docstring.
        }
    }

    /**
     * The stage the PREVIOUS launch reached. Call once, early, before this
     * launch overwrites it. Null on a genuine first run.
     */
    fun previousStage(context: Context): String? = try {
        prefs(context).getString(KEY_CURRENT_STAGE, null)
    } catch (_: Throwable) {
        null
    }

    /**
     * Clears the breadcrumb and resets the consecutive-failure counter. Called
     * only from the first-frame stamp: reaching a rendered frame is the single
     * unambiguous definition of "this launch actually worked".
     */
    fun markLaunchSucceeded(context: Context) {
        try {
            prefs(context).edit()
                .putString(KEY_CURRENT_STAGE, STAGE_FIRST_FRAME)
                .putInt(KEY_CONSECUTIVE_FAILURES, 0)
                .commit()
        } catch (_: Throwable) {
        }
    }

    /**
     * Increments and returns how many launches in a row have failed to reach a
     * first frame. The user in the report saw five consecutive failures, so this
     * counter is what distinguishes "a one-off kill" from "this device can never
     * open the app" without needing the user to tell us.
     */
    fun recordFailedLaunch(context: Context): Int = try {
        val store = prefs(context)
        val next = store.getInt(KEY_CONSECUTIVE_FAILURES, 0) + 1
        store.edit().putInt(KEY_CONSECUTIVE_FAILURES, next).commit()
        next
    } catch (_: Throwable) {
        0
    }

    fun launchCount(context: Context): Int = try {
        val store = prefs(context)
        val next = store.getInt(KEY_LAUNCH_COUNT, 0) + 1
        store.edit().putInt(KEY_LAUNCH_COUNT, next).commit()
        next
    } catch (_: Throwable) {
        0
    }

    /**
     * A random per-install identifier so repeated reports from one broken device
     * can be correlated. Deliberately NOT an advertising ID, NOT the Android ID,
     * and not derived from any hardware identifier: it is meaningless outside
     * this app's own storage and dies with an uninstall.
     */
    fun installId(context: Context): String = try {
        val store = prefs(context)
        store.getString(KEY_INSTALL_ID, null) ?: UUID.randomUUID().toString().also {
            store.edit().putString(KEY_INSTALL_ID, it).commit()
        }
    } catch (_: Throwable) {
        "unknown"
    }
}
