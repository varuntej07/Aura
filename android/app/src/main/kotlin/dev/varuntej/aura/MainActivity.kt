package dev.varuntej.aura

import android.appwidget.AppWidgetManager
import android.content.ComponentName
import android.content.Intent
import android.media.RingtoneManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import androidx.activity.enableEdgeToEdge
import androidx.activity.result.contract.ActivityResultContracts
import dev.varuntej.aura.alarm.AlarmActivity
import dev.varuntej.aura.alarm.AlarmBridge
import dev.varuntej.aura.alarm.AlarmStore
import dev.varuntej.aura.alarm.AlarmTones
import dev.varuntej.aura.diagnostics.StartupBeacon
import dev.varuntej.aura.diagnostics.StartupForensics
import dev.varuntej.aura.diagnostics.StartupTrace
import dev.varuntej.aura.keyboard.KeyboardCredentialStore
import dev.varuntej.aura.keyboard.KeyboardVoiceHandoff
import dev.varuntej.aura.widget.VoiceWidgetProvider
import io.flutter.embedding.android.FlutterFragmentActivity
import io.flutter.embedding.engine.FlutterEngine
import io.flutter.plugin.common.MethodChannel
import java.util.concurrent.Executors

class MainActivity : FlutterFragmentActivity() {

    /** The tone-picker call waiting on a result, if the picker is open. */
    private var pendingTonePick: MethodChannel.Result? = null

    /**
     * The system ringtone picker, for "Choose from device…" in alarm settings.
     *
     * Registered as a field so it exists before onStart, which the Activity
     * Result API requires. A cancelled pick is reported as a success with a
     * blank uri, not as an error: backing out of a picker is a normal thing to
     * do and is not a failure the Flutter side should have to handle twice.
     */
    private val ringtonePicker = registerForActivityResult(
        ActivityResultContracts.StartActivityForResult(),
    ) { activityResult ->
        val call = pendingTonePick
        pendingTonePick = null
        if (call == null) return@registerForActivityResult

        val picked: Uri? = activityResult.data
            ?.getParcelableExtra(RingtoneManager.EXTRA_RINGTONE_PICKED_URI)
        if (activityResult.resultCode == RESULT_OK) {
            // A null uri here is the picker's "Silent" row. Stored as a cleared
            // preference, which resolves back to the system default alarm sound:
            // an alarm the user cannot hear is not a choice this screen offers.
            AlarmStore.putDeviceToneUri(applicationContext, picked?.toString())
        }
        call.success(
            mapOf(
                "uri" to AlarmStore.deviceToneUri(applicationContext).orEmpty(),
                "title" to AlarmTones.deviceToneTitle(applicationContext),
            ),
        )
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        // Startup forensics run FIRST, before edge-to-edge and before
        // super.onCreate, because everything after this line is a candidate for
        // the failure being measured. This reads how far the PREVIOUS launch got
        // and what the OS says killed it — see the diagnostics package.
        captureStartupForensics()

        // FlutterFragmentActivity installs its content view during super.onCreate, so
        // enable edge-to-edge first. This also opts in Android 14 and earlier.
        enableEdgeToEdge()
        super.onCreate(savedInstanceState)
    }

    /**
     * Reads the previous launch's breadcrumb and the OS exit record, then stamps
     * this launch as started.
     *
     * A previous stage of anything other than "first_frame" means the last
     * launch died before rendering, which is the symptom under investigation. In
     * that case the report is beaconed straight out over raw HTTP, because the
     * app may not survive long enough to report it any other way.
     *
     * Wrapped whole in a catch: diagnostics must never be the reason a launch
     * fails.
     */
    private fun captureStartupForensics() {
        try {
            val previousStage = StartupTrace.previousStage(applicationContext)
            val launchCount = StartupTrace.launchCount(applicationContext)
            val isFirstEverLaunch = previousStage == null && launchCount <= 1
            val previousLaunchFailed =
                !isFirstEverLaunch && previousStage != StartupTrace.STAGE_FIRST_FRAME

            StartupTrace.stamp(applicationContext, StartupTrace.STAGE_NATIVE_ON_CREATE)

            if (previousLaunchFailed) {
                val consecutiveFailures = StartupTrace.recordFailedLaunch(applicationContext)
                val report = StartupForensics.buildReport(
                    applicationContext,
                    previousStage,
                    consecutiveFailures,
                    launchCount,
                )
                // Held for Dart to collect over the diagnostics channel, so the
                // same record also lands in Crashlytics as a non-fatal IF the
                // app survives long enough. The beacon does not depend on that.
                pendingForensicsReport = report.toString()
                StartupBeacon.send(report)
            }
        } catch (_: Throwable) {
            // See the diagnostics package docstrings.
        }
    }

    // Lets the Flutter app push the Buddy Keyboard credential (uid + Firebase ID token
    // + active API base URL) into shared secure storage on sign-in / token refresh, and
    // clear it on sign-out. See KeyboardCredentialBridge (Dart) and
    // KeyboardCredentialStore (Kotlin).
    private val keyboardChannel = "dev.varuntej.aura/keyboard"

    // Bridges home-screen widget taps (and any future native launch actions) into
    // Flutter, and lets the app pin the voice widget from its own UI. See
    // VoiceLauncherBridge (Dart) and VoiceWidgetProvider (Kotlin).
    private val widgetChannel = "dev.varuntej.aura/widget"

    // Lets Dart stamp its own startup milestones onto the native breadcrumb and
    // collect the previous launch's forensics. See StartupDiagnosticsService (Dart)
    // and the diagnostics package (Kotlin).
    private val diagnosticsChannel = "dev.varuntej.aura/diagnostics"

    // The previous launch's forensics report, as a JSON string, when that launch
    // failed to reach a first frame. Null on a healthy launch. Handed to Dart once.
    private var pendingForensicsReport: String? = null

    // The launch action carried by the intent that started this activity (e.g. a
    // voice-widget tap). Captured on cold launch and handed to Flutter once via
    // consumeLaunchAction; warm launches push straight through onNewIntent instead.
    private var pendingLaunchAction: String? = null
    private var widgetMethodChannel: MethodChannel? = null

    // The alarm the user answered with "I'm up", as a JSON string, so Dart can
    // open a chat turn seeded with what they had committed to. Handed over once.
    private var pendingAlarmWake: String? = null

    // The keyboard credential / voice-handoff bridges decrypt EncryptedSharedPreferences (an
    // AndroidKeyStore round-trip + disk), which must not run on the platform/main thread. Channel
    // work runs here and the MethodChannel result is posted back on the UI thread.
    private val keyboardBridgeExecutor = Executors.newSingleThreadExecutor()

    override fun configureFlutterEngine(flutterEngine: FlutterEngine) {
        super.configureFlutterEngine(flutterEngine)

        // Alarms. Dart owns the network half (fetching the schedule, flushing
        // acks); Kotlin owns the schedule itself, because it is the only half
        // that exists when an alarm fires at 3 AM. See AlarmBridge.
        AlarmBridge.register(flutterEngine.dartExecutor.binaryMessenger, applicationContext)

        // The one alarm call that needs an Activity rather than an application
        // context: picking a ringtone is a result-returning intent.
        AlarmBridge.deviceTonePicker = { result ->
            // Refuse a second concurrent pick rather than silently dropping the
            // first caller's Result, which would leave its Dart future hanging
            // for the life of the app.
            if (pendingTonePick != null) {
                result.error("busy", "The ringtone picker is already open", null)
            } else {
                pendingTonePick = result
                try {
                    ringtonePicker.launch(buildRingtonePickerIntent())
                } catch (t: Throwable) {
                    pendingTonePick = null
                    result.error("unavailable", "No ringtone picker on this device", null)
                }
            }
        }

        // Reaching here means the Flutter engine was created successfully, which
        // rules out the entire native/engine-load class of startup failure.
        StartupTrace.stamp(applicationContext, StartupTrace.STAGE_ENGINE_CONFIGURED)

        MethodChannel(flutterEngine.dartExecutor.binaryMessenger, diagnosticsChannel)
            .setMethodCallHandler { call, result ->
                when (call.method) {
                    // Dart reports how far ITS half of startup got. Stamped
                    // synchronously to disk so it survives an immediate death.
                    "stampStage" -> {
                        val stage = call.argument<String>("stage")
                        if (stage.isNullOrBlank()) {
                            result.error("invalid_args", "stage is required", null)
                        } else {
                            StartupTrace.stamp(applicationContext, stage)
                            result.success(true)
                        }
                    }
                    // The first rendered frame is the only unambiguous proof that
                    // this launch actually worked, so it is what clears the
                    // breadcrumb and resets the consecutive-failure counter.
                    "markLaunchSucceeded" -> {
                        StartupTrace.markLaunchSucceeded(applicationContext)
                        result.success(true)
                    }
                    // Returns the previous launch's forensics once, or null when
                    // the previous launch was healthy.
                    "consumeStartupForensics" -> {
                        val report = pendingForensicsReport
                        pendingForensicsReport = null
                        result.success(report)
                    }
                    else -> result.notImplemented()
                }
            }

        MethodChannel(flutterEngine.dartExecutor.binaryMessenger, keyboardChannel)
            .setMethodCallHandler { call, result ->
                when (call.method) {
                    "setKeyboardCredential" -> {
                        val uid = call.argument<String>("uid")
                        val idToken = call.argument<String>("idToken")
                        val apiBaseUrl = call.argument<String>("apiBaseUrl")
                        if (uid.isNullOrBlank() || idToken.isNullOrBlank() || apiBaseUrl.isNullOrBlank()) {
                            result.error("invalid_args", "uid, idToken and apiBaseUrl are required", null)
                        } else {
                            // Encrypted write is off the main thread; reply once it lands.
                            keyboardBridgeExecutor.execute {
                                try {
                                    KeyboardCredentialStore.save(
                                        applicationContext,
                                        uid,
                                        idToken,
                                        apiBaseUrl,
                                    )
                                    runOnUiThread { result.success(true) }
                                } catch (_: Exception) {
                                    runOnUiThread {
                                        result.error(
                                            "credential_store_error",
                                            "Unable to save the keyboard credential",
                                            null,
                                        )
                                    }
                                }
                            }
                        }
                    }
                    "clearKeyboardCredential" -> {
                        keyboardBridgeExecutor.execute {
                            try {
                                KeyboardCredentialStore.clear(applicationContext)
                                runOnUiThread { result.success(true) }
                            } catch (_: Exception) {
                                runOnUiThread {
                                    result.error(
                                        "credential_store_error",
                                        "Unable to clear the keyboard credential",
                                        null,
                                    )
                                }
                            }
                        }
                    }
                    else -> result.notImplemented()
                }
            }

        // Capture the cold-launch action before Dart asks for it.
        pendingLaunchAction = readLaunchAction(intent)
        pendingAlarmWake = readAlarmWake(intent)

        val channel = MethodChannel(flutterEngine.dartExecutor.binaryMessenger, widgetChannel)
        widgetMethodChannel = channel
        channel.setMethodCallHandler { call, result ->
            when (call.method) {
                "consumeLaunchAction" -> {
                    val action = pendingLaunchAction
                    pendingLaunchAction = null
                    result.success(action)
                }
                // The alarm the user answered with "I'm up". Null on every other
                // launch. Read alongside consumeLaunchAction on startup.
                "consumeAlarmWake" -> {
                    val wake = pendingAlarmWake
                    pendingAlarmWake = null
                    result.success(wake)
                }
                "isPinVoiceWidgetSupported" -> result.success(isPinVoiceWidgetSupported())
                "requestPinVoiceWidget" -> result.success(requestPinVoiceWidget())
                // The Buddy Keyboard's Voice chip stashed the on-screen text before
                // opening aura://voice; the app reads it once and sends it to the voice
                // agent as screen context. Returns null when there is nothing pending. The
                // read decrypts, so it runs off the main thread and replies on the UI thread.
                "consumeVoiceContext" -> keyboardBridgeExecutor.execute {
                    val context = KeyboardVoiceHandoff.consume(applicationContext)
                    runOnUiThread { result.success(context) }
                }
                // Open the system "Assist & voice input" settings so the user can pick
                // Buddy as their digital assistant (the assist-gesture magic tier).
                "openAssistantSettings" -> result.success(openAssistantSettings())
                else -> result.notImplemented()
            }
        }
    }

    // Warm launch: the app was already running (singleTop) when the widget was
    // tapped. The engine is alive, so push the action straight to Flutter.
    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        val action = readLaunchAction(intent) ?: return
        readAlarmWake(intent)?.let { pendingAlarmWake = it }
        widgetMethodChannel?.invokeMethod("onLaunchAction", action)
    }

    // MainActivity is exported, so any app can put an arbitrary EXTRA_LAUNCH_ACTION on its launch
    // intent. Only forward values we actually handle, so an unrecognized string never reaches the
    // Flutter launch-action handler.
    private fun readLaunchAction(intent: Intent?): String? =
        when (intent?.getStringExtra(EXTRA_LAUNCH_ACTION)) {
            LAUNCH_ACTION_VOICE -> LAUNCH_ACTION_VOICE
            AlarmActivity.LAUNCH_ACTION_IM_UP -> AlarmActivity.LAUNCH_ACTION_IM_UP
            else -> null
        }

    /**
     * The alarm details behind an "I'm up" launch, as JSON, or null.
     *
     * Bounded and treated as untrusted for the same reason readLaunchAction has a
     * whitelist: this activity is exported, so any app can hand it these extras.
     * The worst case is a chat seeded with attacker-supplied text, which is why
     * the message is length-capped and nothing here is executed or trusted as an
     * identifier beyond looking up a reminder the signed-in user already owns.
     */
    private fun readAlarmWake(intent: Intent?): String? {
        if (intent?.getStringExtra(EXTRA_LAUNCH_ACTION) != AlarmActivity.LAUNCH_ACTION_IM_UP) {
            return null
        }
        val reminderId = intent.getStringExtra(AlarmActivity.EXTRA_REMINDER_ID)
            ?.take(128)
            ?.takeIf { it.isNotBlank() }
            ?: return null
        val message = intent.getStringExtra(AlarmActivity.EXTRA_MESSAGE)?.take(500).orEmpty()
        return org.json.JSONObject()
            .put("reminder_id", reminderId)
            .put("message", message)
            .toString()
    }

    override fun onDestroy() {
        keyboardBridgeExecutor.shutdown()
        super.onDestroy()
    }

    // Open the system assistant picker so the user can choose Buddy. Returns false if no
    // settings screen handles it (then the app can show manual instructions).
    private fun openAssistantSettings(): Boolean = try {
        startActivity(
            Intent(android.provider.Settings.ACTION_VOICE_INPUT_SETTINGS)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
        )
        true
    } catch (t: Throwable) {
        try {
            startActivity(
                Intent(android.provider.Settings.ACTION_SETTINGS)
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
            )
            true
        } catch (_: Throwable) {
            false
        }
    }

    // Whether the current launcher supports app-initiated widget pinning (API 26+).
    private fun isPinVoiceWidgetSupported(): Boolean {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return false
        return AppWidgetManager.getInstance(applicationContext).isRequestPinAppWidgetSupported
    }

    // Ask the launcher to pin the voice widget. Returns false (so the app can show
    // manual instructions instead) when pinning isn't supported.
    private fun requestPinVoiceWidget(): Boolean {
        if (!isPinVoiceWidgetSupported()) return false
        val manager = AppWidgetManager.getInstance(applicationContext)
        val provider = ComponentName(applicationContext, VoiceWidgetProvider::class.java)
        return manager.requestPinAppWidget(provider, null, null)
    }

    /**
     * Opens on the alarm tab with the current pick selected.
     *
     * SHOW_SILENT is off deliberately: this picker exists to choose what wakes
     * you, and "silent" is not an answer to that question. SHOW_DEFAULT is on so
     * the user can always get back to the phone's own alarm sound.
     */
    private fun buildRingtonePickerIntent(): Intent {
        val current = AlarmStore.deviceToneUri(applicationContext)
        return Intent(RingtoneManager.ACTION_RINGTONE_PICKER).apply {
            putExtra(RingtoneManager.EXTRA_RINGTONE_TYPE, RingtoneManager.TYPE_ALARM)
            putExtra(RingtoneManager.EXTRA_RINGTONE_TITLE, "Choose an alarm sound")
            putExtra(RingtoneManager.EXTRA_RINGTONE_SHOW_SILENT, false)
            putExtra(RingtoneManager.EXTRA_RINGTONE_SHOW_DEFAULT, true)
            putExtra(
                RingtoneManager.EXTRA_RINGTONE_EXISTING_URI,
                current?.let { runCatching { Uri.parse(it) }.getOrNull() },
            )
        }
    }

    companion object {
        const val EXTRA_LAUNCH_ACTION = "aura_launch_action"
        const val LAUNCH_ACTION_VOICE = "voice"
    }
}
