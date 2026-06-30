import 'dart:async';

import 'package:app_links/app_links.dart';
import 'package:flutter/foundation.dart';

import '../../core/logging/app_logger.dart';
import 'voice_launcher_bridge.dart';

/// Routes `aura://voice` (and the `https://auravoiceapp.com/voice` App/Universal
/// Link) into the app's voice session. This is the single deep-link primitive every
/// Voice Launcher surface targets: Siri shortcuts, the iOS Action Button / Control
/// Center / widgets, the Android Quick Settings tile and default-assistant role, and
/// plain links shared in a chat or email.
///
/// It deliberately speaks the SAME launch-action vocabulary as [VoiceLauncherBridge]
/// (`launchActionVoice`), so a deep link and a home-screen widget tap funnel into the
/// one handler in HomeScreen. Cold starts are read once via [consumeInitialLaunchAction];
/// warm links arrive on the [launchActions] stream. Cross-platform (the Android widget
/// uses an intent extra, but the deep link works on both Android and iOS).
class DeepLinkService {
  DeepLinkService._();
  static final DeepLinkService instance = DeepLinkService._();

  // Visible for tests (so a fake AppLinks can be injected).
  @visibleForTesting
  AppLinks appLinks = AppLinks();

  final _launchActionController = StreamController<String>.broadcast();
  StreamSubscription<Uri>? _uriSub;
  bool _started = false;

  /// Warm-launch actions parsed from incoming links while the app is running.
  Stream<String> get launchActions => _launchActionController.stream;

  /// Begins listening for warm deep links. Idempotent.
  void start() {
    if (_started) return;
    _started = true;
    _uriSub = appLinks.uriLinkStream.listen(
      (uri) {
        final action = _actionForUri(uri);
        if (action != null) _launchActionController.add(action);
      },
      onError: (Object e) => AppLogger.warning(
        'deep link stream error',
        tag: 'DeepLink',
        metadata: {'error': e.toString()},
      ),
    );
  }

  /// Reads the link that cold-started the app, if any, and maps it to a launch
  /// action. Returns null when the app was opened normally. Call once on startup.
  Future<String?> consumeInitialLaunchAction() async {
    try {
      final uri = await appLinks.getInitialLink();
      return uri == null ? null : _actionForUri(uri);
    } catch (e) {
      AppLogger.warning(
        'getInitialLink failed',
        tag: 'DeepLink',
        metadata: {'error': e.toString()},
      );
      return null;
    }
  }

  String? _actionForUri(Uri uri) => actionForUri(uri);

  /// Maps a deep link to a launch action, or null if it is not one we handle.
  /// Accepts `aura://voice` and the `auravoiceapp.com/voice` http(s) App Link path.
  /// The host is checked so a stray `/voice` on any other site never opens voice.
  /// Static and pure so it can be unit-tested without the app_links plugin.
  @visibleForTesting
  static String? actionForUri(Uri uri) {
    final isAuraScheme = uri.scheme == 'aura' && uri.host == 'voice';
    // A trailing slash (.../voice/) yields an empty final segment; drop it so the
    // App Link still matches.
    final raw = uri.pathSegments;
    final segments = (raw.isNotEmpty && raw.last.isEmpty)
        ? raw.sublist(0, raw.length - 1)
        : raw;
    final isVoicePath = uri.host == 'auravoiceapp.com' &&
        segments.isNotEmpty &&
        segments.last == 'voice';
    if (isAuraScheme || isVoicePath) return VoiceLauncherBridge.launchActionVoice;
    return null;
  }

  void dispose() {
    _uriSub?.cancel();
    _launchActionController.close();
  }
}
