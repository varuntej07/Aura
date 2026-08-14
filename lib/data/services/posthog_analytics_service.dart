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

  /// Event fired on every launch, immediately after setup.
  ///
  /// This is a deliberate heartbeat, not a duplicate of PostHog's automatic
  /// "Application Opened". `AUTO_INIT` is false in the manifest, so the SDK is
  /// configured from Dart AFTER the Activity has already started, which means
  /// the `ProcessLifecycleOwner` hooks behind `captureApplicationLifecycleEvents`
  /// can miss the foreground transition that already happened. An explicit
  /// capture cannot miss it.
  ///
  /// It is also the distinguishing probe for the current blind spot, where
  /// PostHog shows no events at all: if [launchEvent] arrives and
  /// "Application Opened" does not, the lifecycle hooks are the problem. If
  /// NEITHER arrives, the SDK is not reaching PostHog and the cause is the
  /// project token, the region host, or the network — not this app's call sites.
  static const String launchEvent = 'app_launched';

  /// Initialize the PostHog SDK explicitly from Dart.
  ///
  /// Throws on failure rather than swallowing. The caller in `main.dart` runs
  /// this inside a guarded startup step that records the error to Crashlytics
  /// and lets the app boot anyway — analytics must never be silently dead AND
  /// never block startup. Zero events and healthy must not look identical.
  static Future<void> initialize() async {
    final config = PostHogConfig(projectToken)
      ..host = host
      ..captureApplicationLifecycleEvents = true
      ..debug = !kReleaseMode;
    await Posthog().setup(config);

    // Guaranteed heartbeat. Carries the build mode because the whole reason
    // production telemetry went missing was a release binary behaving like a
    // dev one, and that must be visible in the data itself from now on.
    await Posthog().capture(
      eventName: launchEvent,
      properties: {
        'build_mode': kReleaseMode
            ? 'release'
            : kProfileMode
            ? 'profile'
            : 'debug',
      },
    );
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
