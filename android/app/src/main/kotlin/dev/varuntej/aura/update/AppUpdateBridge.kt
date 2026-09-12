package dev.varuntej.aura.update

import android.app.Activity
import androidx.activity.result.ActivityResultLauncher
import androidx.activity.result.IntentSenderRequest
import androidx.fragment.app.FragmentActivity
import com.google.android.play.core.appupdate.AppUpdateInfo
import com.google.android.play.core.appupdate.AppUpdateManager
import com.google.android.play.core.appupdate.AppUpdateManagerFactory
import com.google.android.play.core.appupdate.AppUpdateOptions
import com.google.android.play.core.install.InstallStateUpdatedListener
import com.google.android.play.core.install.model.AppUpdateType
import com.google.android.play.core.install.model.InstallErrorCode
import com.google.android.play.core.install.model.InstallStatus
import com.google.android.play.core.install.model.UpdateAvailability
import io.flutter.plugin.common.BinaryMessenger
import io.flutter.plugin.common.MethodChannel

/**
 * Small Flutter bridge over Google Play's official in-app update API.
 *
 * Google Play owns update eligibility, consent, download, signature validation,
 * and installation. Flutter only decides when to surface Aura's prompt and when
 * the user is ready for the restart that completes a downloaded flexible update.
 */
class AppUpdateBridge(
    activity: FragmentActivity,
    messenger: BinaryMessenger,
    private val updateLauncher: ActivityResultLauncher<IntentSenderRequest>,
) {
    private val manager: AppUpdateManager = AppUpdateManagerFactory.create(activity)
    private val channel = MethodChannel(messenger, CHANNEL)
    private var cachedUpdateInfo: AppUpdateInfo? = null
    private var pendingStartResult: MethodChannel.Result? = null
    private var disposed = false

    private val installStateListener = InstallStateUpdatedListener { state ->
        if (disposed) return@InstallStateUpdatedListener
        when {
            state.installStatus() == InstallStatus.DOWNLOADED -> {
                channel.invokeMethod(EVENT_UPDATE_DOWNLOADED, null)
            }
            state.installErrorCode() != InstallErrorCode.NO_ERROR -> {
                channel.invokeMethod(
                    EVENT_UPDATE_FAILED,
                    mapOf("errorCode" to state.installErrorCode()),
                )
            }
        }
    }

    init {
        manager.registerListener(installStateListener)
        channel.setMethodCallHandler { call, result ->
            when (call.method) {
                METHOD_CHECK -> checkForUpdate(result)
                METHOD_START_FLEXIBLE -> startFlexibleUpdate(result)
                METHOD_COMPLETE_FLEXIBLE -> completeFlexibleUpdate(result)
                else -> result.notImplemented()
            }
        }
    }

    /** Re-checks completion state whenever Aura returns to the foreground. */
    fun onResume() {
        if (disposed) return
        manager.appUpdateInfo.addOnSuccessListener { info ->
            cachedUpdateInfo = info
            if (!disposed && info.installStatus() == InstallStatus.DOWNLOADED) {
                channel.invokeMethod(EVENT_UPDATE_DOWNLOADED, null)
            }
        }
    }

    /** Completes the pending Dart call after Google's consent UI closes. */
    fun onUpdateFlowResult(resultCode: Int) {
        val result = pendingStartResult ?: return
        pendingStartResult = null
        when (resultCode) {
            Activity.RESULT_OK -> result.success(START_ACCEPTED)
            Activity.RESULT_CANCELED -> result.success(START_CANCELLED)
            com.google.android.play.core.install.model.ActivityResult.RESULT_IN_APP_UPDATE_FAILED ->
                result.error("update_failed", "Google Play could not start the update", null)
            else -> result.error(
                "unexpected_result",
                "Google Play returned update result $resultCode",
                null,
            )
        }
    }

    fun dispose() {
        disposed = true
        manager.unregisterListener(installStateListener)
        channel.setMethodCallHandler(null)
        pendingStartResult?.error(
            "activity_destroyed",
            "Aura closed before the update prompt completed",
            null,
        )
        pendingStartResult = null
        cachedUpdateInfo = null
    }

    private fun checkForUpdate(result: MethodChannel.Result) {
        manager.appUpdateInfo
            .addOnSuccessListener { info ->
                cachedUpdateInfo = info
                val state = when {
                    info.installStatus() == InstallStatus.DOWNLOADED -> STATE_DOWNLOADED
                    info.updateAvailability() == UpdateAvailability.UPDATE_AVAILABLE &&
                        info.isUpdateTypeAllowed(AppUpdateType.FLEXIBLE) -> STATE_AVAILABLE
                    else -> STATE_NONE
                }
                result.success(
                    mapOf(
                        "state" to state,
                        "availableVersionCode" to info.availableVersionCode(),
                        "stalenessDays" to info.clientVersionStalenessDays(),
                        "priority" to info.updatePriority(),
                    ),
                )
            }
            .addOnFailureListener { error ->
                result.error(
                    "check_failed",
                    error.message ?: "Google Play update check failed",
                    null,
                )
            }
    }

    private fun startFlexibleUpdate(result: MethodChannel.Result) {
        if (pendingStartResult != null) {
            result.error("busy", "An update prompt is already open", null)
            return
        }
        val info = cachedUpdateInfo
        if (
            info == null ||
            info.updateAvailability() != UpdateAvailability.UPDATE_AVAILABLE ||
            !info.isUpdateTypeAllowed(AppUpdateType.FLEXIBLE)
        ) {
            result.error("not_available", "No flexible update is currently available", null)
            return
        }

        pendingStartResult = result
        try {
            val started = manager.startUpdateFlowForResult(
                info,
                updateLauncher,
                AppUpdateOptions.newBuilder(AppUpdateType.FLEXIBLE).build(),
            )
            if (!started) {
                pendingStartResult = null
                result.error("not_started", "Google Play declined to start the update", null)
            }
        } catch (error: Throwable) {
            pendingStartResult = null
            result.error(
                "start_failed",
                error.message ?: "Google Play could not open the update prompt",
                null,
            )
        }
    }

    private fun completeFlexibleUpdate(result: MethodChannel.Result) {
        manager.completeUpdate()
            .addOnSuccessListener { result.success(true) }
            .addOnFailureListener { error ->
                result.error(
                    "install_failed",
                    error.message ?: "Google Play could not install the downloaded update",
                    null,
                )
            }
    }

    companion object {
        const val CHANNEL = "dev.varuntej.aura/app_update"

        private const val METHOD_CHECK = "checkForUpdate"
        private const val METHOD_START_FLEXIBLE = "startFlexibleUpdate"
        private const val METHOD_COMPLETE_FLEXIBLE = "completeFlexibleUpdate"

        private const val EVENT_UPDATE_DOWNLOADED = "updateDownloaded"
        private const val EVENT_UPDATE_FAILED = "updateFailed"

        private const val STATE_NONE = "none"
        private const val STATE_AVAILABLE = "available"
        private const val STATE_DOWNLOADED = "downloaded"

        private const val START_ACCEPTED = "accepted"
        private const val START_CANCELLED = "cancelled"
    }
}
