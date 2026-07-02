import 'dart:async';

import 'package:flutter/foundation.dart';
import 'package:posthog_flutter/posthog_flutter.dart';

import '../../core/analytics/analytics_client.dart';

class PostHogAnalyticsService implements AnalyticsClient {
  PostHogAnalyticsService();

  /// Public PostHog project token. This is a client-side key, safe to embed in
  /// the app. Public so the desktop HTTP capture client reuses the same
  /// project without duplicating the token.
  static const String projectToken = 'phc_CDtz3DmNraHdnJ2w9W7WJNkJ8VANYPBWAcqV2Uf77k5s';

  /// PostHog US-cloud ingestion host. Must match the region of the project that owns [projectToken];
  static const String host = 'https://us.i.posthog.com';

  /// Initialize the PostHog SDK explicitly from Dart
  static Future<void> initialize() async {
    final config = PostHogConfig(projectToken)
      ..host = host
      ..captureApplicationLifecycleEvents = true
      ..debug = !kReleaseMode;
    await Posthog().setup(config);
  }

  @override
  Future<void> trackEvent(String event, {Map<String, Object>? properties}) async {
    await Posthog().capture(eventName: event, properties: properties);
  }

  @override
  Future<void> identifyUser(String uid, {Map<String, Object>? traits}) async {
    await Posthog().identify(userId: uid, userProperties: traits);
  }

  @override
  Future<void> screenView(String screenName) async {
    await Posthog().screen(screenName: screenName);
  }

  @override
  Future<void> reset() async {
    await Posthog().reset();
  }
}
