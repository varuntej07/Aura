package dev.varuntej.aura

import android.appwidget.AppWidgetManager
import android.content.ComponentName
import android.content.Intent
import android.os.Build
import dev.varuntej.aura.keyboard.KeyboardCredentialStore
import dev.varuntej.aura.keyboard.KeyboardVoiceHandoff
import dev.varuntej.aura.widget.VoiceWidgetProvider
import io.flutter.embedding.android.FlutterActivity
import io.flutter.embedding.engine.FlutterEngine
import io.flutter.plugin.common.MethodChannel
import java.util.concurrent.Executors

class MainActivity : FlutterActivity() {

    // Lets the Flutter app push the Buddy Keyboard credential (uid + Firebase ID token
    // + active API base URL) into shared secure storage on sign-in / token refresh, and
    // clear it on sign-out. See KeyboardCredentialBridge (Dart) and
    // KeyboardCredentialStore (Kotlin).
    private val keyboardChannel = "dev.varuntej.aura/keyboard"

    // Bridges home-screen widget taps (and any future native launch actions) into
    // Flutter, and lets the app pin the voice widget from its own UI. See
    // VoiceLauncherBridge (Dart) and VoiceWidgetProvider (Kotlin).
    private val widgetChannel = "dev.varuntej.aura/widget"

    // The launch action carried by the intent that started this activity (e.g. a
    // voice-widget tap). Captured on cold launch and handed to Flutter once via
    // consumeLaunchAction; warm launches push straight through onNewIntent instead.
    private var pendingLaunchAction: String? = null
    private var widgetMethodChannel: MethodChannel? = null

    // The keyboard credential / voice-handoff bridges decrypt EncryptedSharedPreferences (an
    // AndroidKeyStore round-trip + disk), which must not run on the platform/main thread. Channel
    // work runs here and the MethodChannel result is posted back on the UI thread.
    private val keyboardBridgeExecutor = Executors.newSingleThreadExecutor()

    override fun configureFlutterEngine(flutterEngine: FlutterEngine) {
        super.configureFlutterEngine(flutterEngine)

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
                                KeyboardCredentialStore.save(applicationContext, uid, idToken, apiBaseUrl)
                                runOnUiThread { result.success(true) }
                            }
                        }
                    }
                    "clearKeyboardCredential" -> {
                        keyboardBridgeExecutor.execute {
                            KeyboardCredentialStore.clear(applicationContext)
                            runOnUiThread { result.success(true) }
                        }
                    }
                    else -> result.notImplemented()
                }
            }

        // Capture the cold-launch action before Dart asks for it.
        pendingLaunchAction = readLaunchAction(intent)

        val channel = MethodChannel(flutterEngine.dartExecutor.binaryMessenger, widgetChannel)
        widgetMethodChannel = channel
        channel.setMethodCallHandler { call, result ->
            when (call.method) {
                "consumeLaunchAction" -> {
                    val action = pendingLaunchAction
                    pendingLaunchAction = null
                    result.success(action)
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
        widgetMethodChannel?.invokeMethod("onLaunchAction", action)
    }

    // MainActivity is exported, so any app can put an arbitrary EXTRA_LAUNCH_ACTION on its launch
    // intent. Only forward values we actually handle, so an unrecognized string never reaches the
    // Flutter launch-action handler.
    private fun readLaunchAction(intent: Intent?): String? =
        when (intent?.getStringExtra(EXTRA_LAUNCH_ACTION)) {
            LAUNCH_ACTION_VOICE -> LAUNCH_ACTION_VOICE
            else -> null
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

    companion object {
        const val EXTRA_LAUNCH_ACTION = "aura_launch_action"
        const val LAUNCH_ACTION_VOICE = "voice"
    }
}
