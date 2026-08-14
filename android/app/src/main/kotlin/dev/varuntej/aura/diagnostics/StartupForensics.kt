package dev.varuntej.aura.diagnostics

import android.app.ActivityManager
import android.app.ApplicationExitInfo
import android.content.Context
import android.content.pm.PackageManager
import android.os.Build
import org.json.JSONArray
import org.json.JSONObject

/**
 * Reads the OPERATING SYSTEM's own record of why previous app processes died.
 *
 * This exists because every other reporting channel has a blind spot that this
 * app has already fallen into:
 *
 *  * Crashlytics needs to be initialised. If the process dies before Dart's
 *    `main()` gets far enough to arm it, there is nothing to report with.
 *  * Play Console's Android vitals only aggregates from users who opted into
 *    sharing diagnostics, and needs volume before it surfaces anything.
 *  * A logcat or bug report needs a cooperative, technical user with a cable.
 *
 * `ActivityManager.getHistoricalProcessExitReasons` has none of those
 * constraints. The OS records it unconditionally, it survives process death and
 * reboots, and it is readable by the app itself on the next launch. It is the
 * only instrument that can explain a death that happens before our own code is
 * capable of speaking.
 *
 * Requires API 30 (Android 11). On older devices this returns an empty list and
 * the boot-stage breadcrumb in [StartupTrace] carries the diagnosis alone.
 */
object StartupForensics {

    private const val MAX_EXIT_RECORDS = 10

    /**
     * Human-readable name for an [ApplicationExitInfo] reason code. The raw ints
     * are meaningless in a dashboard, and this is the field that actually
     * answers "did it crash, did it ANR, or did something kill it".
     */
    private fun reasonName(reason: Int): String = when (reason) {
        ApplicationExitInfo.REASON_ANR -> "anr"
        ApplicationExitInfo.REASON_CRASH -> "crash_jvm"
        ApplicationExitInfo.REASON_CRASH_NATIVE -> "crash_native"
        ApplicationExitInfo.REASON_DEPENDENCY_DIED -> "dependency_died"
        ApplicationExitInfo.REASON_EXCESSIVE_RESOURCE_USAGE -> "excessive_resource_usage"
        ApplicationExitInfo.REASON_EXIT_SELF -> "exit_self"
        ApplicationExitInfo.REASON_INITIALIZATION_FAILURE -> "initialization_failure"
        ApplicationExitInfo.REASON_LOW_MEMORY -> "low_memory"
        ApplicationExitInfo.REASON_OTHER -> "other"
        ApplicationExitInfo.REASON_PERMISSION_CHANGE -> "permission_change"
        ApplicationExitInfo.REASON_SIGNALED -> "signaled"
        ApplicationExitInfo.REASON_USER_REQUESTED -> "user_requested"
        ApplicationExitInfo.REASON_USER_STOPPED -> "user_stopped"
        else -> "unknown_$reason"
    }

    /**
     * Every recorded process exit, newest first, as plain JSON-ready maps.
     *
     * Deliberately does NOT read `ApplicationExitInfo.traceInputStream`: on an
     * ANR that is a full thread dump, which is both large and capable of
     * containing user-visible strings. `description` carries the OS's own short
     * explanation, which is what we actually need to classify the failure.
     */
    fun exitRecords(context: Context): List<Map<String, Any?>> {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.R) return emptyList()
        return try {
            val activityManager =
                context.getSystemService(Context.ACTIVITY_SERVICE) as? ActivityManager
                    ?: return emptyList()
            activityManager
                .getHistoricalProcessExitReasons(context.packageName, 0, MAX_EXIT_RECORDS)
                .map { info ->
                    mapOf(
                        "reason" to reasonName(info.reason),
                        "reason_code" to info.reason,
                        // For REASON_SIGNALED this is the signal number (11 = SIGSEGV);
                        // for REASON_CRASH it is the exit status.
                        "status" to info.status,
                        "description" to info.description,
                        "importance" to info.importance,
                        "timestamp_ms" to info.timestamp,
                        "pss_kb" to info.pss,
                        "rss_kb" to info.rss,
                        "process_name" to info.processName,
                    )
                }
        } catch (_: Throwable) {
            emptyList()
        }
    }

    /** Device and build identity, so a report identifies WHICH phone is broken. */
    fun deviceInfo(context: Context): Map<String, Any?> {
        val (versionName, versionCode) = versionInfo(context)
        return mapOf(
            "manufacturer" to Build.MANUFACTURER,
            "brand" to Build.BRAND,
            "model" to Build.MODEL,
            "device" to Build.DEVICE,
            "sdk_int" to Build.VERSION.SDK_INT,
            "release" to Build.VERSION.RELEASE,
            // A mismatch between the device's ABI and what Play actually
            // delivered is a live hypothesis for a launch-time native failure,
            // so record both what the device supports and what we shipped.
            "supported_abis" to Build.SUPPORTED_ABIS.toList(),
            "app_version" to versionName,
            "app_build" to versionCode,
            "installer" to installerPackage(context),
        )
    }

    private fun versionInfo(context: Context): Pair<String?, Long> = try {
        val packageInfo = context.packageManager.getPackageInfo(context.packageName, 0)
        val code = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
            packageInfo.longVersionCode
        } else {
            @Suppress("DEPRECATION")
            packageInfo.versionCode.toLong()
        }
        packageInfo.versionName to code
    } catch (_: Throwable) {
        null to -1L
    }

    /**
     * Which store installed this build. `null` means a sideload, which would
     * itself explain a missing split APK.
     */
    private fun installerPackage(context: Context): String? = try {
        val manager: PackageManager = context.packageManager
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            manager.getInstallSourceInfo(context.packageName).installingPackageName
        } else {
            @Suppress("DEPRECATION")
            manager.getInstallerPackageName(context.packageName)
        }
    } catch (_: Throwable) {
        null
    }

    /**
     * The full report for a launch that follows a failed one: what the OS says
     * killed us, how far the previous launch got, and which device this is.
     */
    fun buildReport(
        context: Context,
        previousStage: String?,
        consecutiveFailures: Int,
        launchCount: Int,
    ): JSONObject = JSONObject().apply {
        put("install_id", StartupTrace.installId(context))
        put("previous_stage", previousStage ?: JSONObject.NULL)
        put("consecutive_failed_launches", consecutiveFailures)
        put("launch_count", launchCount)
        put("device", JSONObject(deviceInfo(context)))
        put("exits", JSONArray(exitRecords(context).map { JSONObject(it) }))
    }
}
