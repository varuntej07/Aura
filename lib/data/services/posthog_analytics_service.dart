import 'dart:async';

import 'package:posthog_flutter/posthog_flutter.dart';

class PostHogAnalyticsService {
  PostHogAnalyticsService();

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
