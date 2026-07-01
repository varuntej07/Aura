import 'dart:async';

import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';

import '../../core/logging/app_logger.dart';
import '../models/voice_models.dart';

/// Bridges home-screen widget taps into the Flutter app.
///
/// The Aura voice widget launches MainActivity with a launch-action extra (see
/// VoiceWidgetProvider / MainActivity on the native side). This service reads that
/// action on a cold start ([consumePendingLaunchAction]) and relays warm starts
/// (the [launchActions] stream, fired from MainActivity.onNewIntent) so the home
/// surface can open straight into a live voice session.
///
/// It also exposes [requestPinVoiceWidget] so the app can offer "Add to home
/// screen" in-app (Android 8.0+ requestPinAppWidget).
///
/// Android-only: the iOS voice widget (WidgetKit) ships separately and uses a URL
/// scheme instead, wired when that target lands. Every call is a graceful no-op on
/// other platforms.
class VoiceLauncherBridge {
  VoiceLauncherBridge._();
  static final VoiceLauncherBridge instance = VoiceLauncherBridge._();

  static const MethodChannel _channel = MethodChannel('dev.varuntej.aura/widget');

  /// Launch-action value the native side sends for "open with mic on". Must match
  /// MainActivity.LAUNCH_ACTION_VOICE.
  static const String launchActionVoice = 'voice';

  final _launchActionController = StreamController<String>.broadcast();
  bool _started = false;

  /// Warm-launch actions pushed from native (the app was already running when the
  /// widget was tapped). Broadcast so the home surface can react from any route.
  Stream<String> get launchActions => _launchActionController.stream;

  /// Begins relaying warm-launch actions from native. Idempotent; Android-only.
  void start() {
    if (_started) return;
    if (defaultTargetPlatform != TargetPlatform.android) return;
    _channel.setMethodCallHandler(_onNativeCall);
    _started = true;
  }

  Future<dynamic> _onNativeCall(MethodCall call) async {
    if (call.method == 'onLaunchAction') {
      final action = call.arguments as String?;
      if (action != null && action.isNotEmpty) {
        _launchActionController.add(action);
      }
    }
  }

  /// Reads (and clears) the launch action carried by the intent that cold-started
  /// the app. Returns null if the app was opened normally. Call once on startup.
  Future<String?> consumePendingLaunchAction() async {
    if (defaultTargetPlatform != TargetPlatform.android) return null;
    try {
      return await _channel.invokeMethod<String>('consumeLaunchAction');
    } on PlatformException catch (e) {
      AppLogger.warning(
        'consumeLaunchAction failed',
        tag: 'VoiceLauncher',
        metadata: {'error': e.message ?? e.code},
      );
      return null;
    } on MissingPluginException {
      // No native handler (e.g. in a widget test); treat as a normal open.
      return null;
    }
  }

  /// Reads (and clears) the on-screen text the Buddy Keyboard stashed before opening
  /// aura://voice, so the app can hand it to the voice agent as screen context. Returns
  /// null when nothing is pending (a normal mic tap or widget launch) or it has gone
  /// stale. Android-only; a no-op elsewhere.
  Future<ScreenContextHandoff?> consumePendingVoiceContext() async {
    if (defaultTargetPlatform != TargetPlatform.android) return null;
    try {
      final map = await _channel.invokeMapMethod<String, dynamic>('consumeVoiceContext');
      final text = (map?['text'] as String?)?.trim() ?? '';
      if (text.isEmpty) return null;
      return ScreenContextHandoff(
        text: text,
        fieldType: map?['field_type'] as String?,
        app: map?['app'] as String?,
      );
    } on PlatformException catch (e) {
      AppLogger.warning('consumeVoiceContext failed', tag: 'VoiceLauncher',
          metadata: {'error': e.message ?? e.code});
      return null;
    } on MissingPluginException {
      return null;
    }
  }

  /// Whether the launcher supports app-initiated widget pinning (Android 8.0+).
  Future<bool> isPinVoiceWidgetSupported() async {
    if (defaultTargetPlatform != TargetPlatform.android) return false;
    try {
      return await _channel.invokeMethod<bool>('isPinVoiceWidgetSupported') ?? false;
    } on PlatformException {
      return false;
    } on MissingPluginException {
      return false;
    }
  }

  /// Opens the system "Assist & voice input" settings so the user can pick Buddy as
  /// their digital assistant (the assist-gesture entry point). Returns true if a
  /// settings screen opened. Android-only.
  Future<bool> openAssistantSettings() async {
    if (defaultTargetPlatform != TargetPlatform.android) return false;
    try {
      return await _channel.invokeMethod<bool>('openAssistantSettings') ?? false;
    } on PlatformException catch (e) {
      AppLogger.warning('openAssistantSettings failed', tag: 'VoiceLauncher',
          metadata: {'error': e.message ?? e.code});
      return false;
    } on MissingPluginException {
      return false;
    }
  }

  /// Asks the launcher to pin the Aura voice widget to the home screen. Returns
  /// true when the request was accepted (the launcher then shows its own confirm
  /// UI); false when pinning isn't supported, so the caller can fall back to
  /// manual instructions.
  Future<bool> requestPinVoiceWidget() async {
    if (defaultTargetPlatform != TargetPlatform.android) return false;
    try {
      return await _channel.invokeMethod<bool>('requestPinVoiceWidget') ?? false;
    } on PlatformException catch (e) {
      AppLogger.warning(
        'requestPinVoiceWidget failed',
        tag: 'VoiceLauncher',
        metadata: {'error': e.message ?? e.code},
      );
      return false;
    } on MissingPluginException {
      return false;
    }
  }
}
