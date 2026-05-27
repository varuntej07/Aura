import 'dart:async';

import 'package:posthog_flutter/posthog_flutter.dart';

import '../../core/config/environment.dart';

class PostHogAnalyticsService {
  PostHogAnalyticsService();

  bool get _canLog => !Environment.isDev;

  Future<void> trackEvent(String event, {Map<String, Object>? properties}) async {
    if (!_canLog) return;
    await Posthog().capture(eventName: event, properties: properties);
  }

  Future<void> identifyUser(String uid, {Map<String, Object>? traits}) async {
    if (!_canLog) return;
    await Posthog().identify(userId: uid, userProperties: traits);
  }

  Future<void> screenView(String screenName) async {
    if (!_canLog) return;
    await Posthog().screen(screenName: screenName);
  }

  Future<void> reset() async {
    if (!_canLog) return;
    await Posthog().reset();
  }
}
