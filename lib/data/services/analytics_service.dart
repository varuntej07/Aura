import 'package:firebase_analytics/firebase_analytics.dart';

import '../../core/config/environment.dart';
import '../../core/config/firebase_runtime.dart';

/// Thin wrapper around Firebase Analytics.
///
/// All methods are fire-and-forget — call with [unawaited] so analytics never
/// blocks UI. Silently no-ops in dev mode and when Firebase is not initialised.
/// Event schema is intentionally minimal at launch. Could add parameters here when in need of BigQuery
class AnalyticsService {
  AnalyticsService._();

  static bool get _canLog => FirebaseRuntime.hasApp && !Environment.isDev;

  /// Events

  /// Logged once per cold start after Firebase is ready.
  /// Firebase also auto-collects `app_open`, but the explicit call confirms
  /// that the Analytics SDK initialised correctly.
  static Future<void> logAppOpen() async {
    if (!_canLog) return;
    await FirebaseAnalytics.instance.logAppOpen();
  }

  /// Logged when the user opens an agent thread screen.
  /// [agentId] matches the string used in the backend (e.g. "sports", "posts").
  /// [agentName] is the display name shown in the UI
  static Future<void> logAgentSelected(
    String agentId,
    String agentName,
  ) async {
    if (!_canLog) return;
    await FirebaseAnalytics.instance.logEvent(
      name: 'agent_selected',
      parameters: {
        'agent_id': agentId,
        'agent_name': agentName,
      },
    );
  }

  /// Logged when a streaming response completes successfully (not on error or
  /// cancellation). [agentId] is "general" for the main Buddy chat screen.
  static Future<void> logMessageSent(String agentId) async {
    if (!_canLog) return;
    await FirebaseAnalytics.instance.logEvent(
      name: 'message_sent',
      parameters: {'agent_id': agentId},
    );
  }

  /// Logged when the LiveKit mic is enabled and the voice session is live.
  static Future<void> logVoiceStarted() async {
    if (!_canLog) return;
    await FirebaseAnalytics.instance.logEvent(name: 'voice_started');
  }
}
