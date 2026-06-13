import 'dart:async';

import 'package:flutter/foundation.dart';
import 'package:posthog_flutter/posthog_flutter.dart';

class PostHogAnalyticsService {
  PostHogAnalyticsService();

  /// Public PostHog project token. This is a client-side key, safe to embed in the app
  static const String _projectToken = 'phc_CDtz3DmNraHdnJ2w9W7WJNkJ8VANYPBWAcqV2Uf77k5s';

  /// PostHog US-cloud ingestion host. Must match the region of the project that owns [_projectToken];
  static const String _host = 'https://us.i.posthog.com';

  /// Initialize the PostHog SDK explicitly from Dart
  static Future<void> initialize() async {
    final config = PostHogConfig(_projectToken)
      ..host = _host
      ..captureApplicationLifecycleEvents = true
      ..debug = !kReleaseMode;
    await Posthog().setup(config);
  }

  Future<void> trackEvent(String event, {Map<String, Object>? properties}) async {
    await Posthog().capture(eventName: event, properties: properties);
  }

  Future<void> identifyUser(String uid, {Map<String, Object>? traits}) async {
    await Posthog().identify(userId: uid, userProperties: traits);
  }

  Future<void> screenView(String screenName) async {
    await Posthog().screen(screenName: screenName);
  }

  Future<void> reset() async {
    await Posthog().reset();
  }
}
