import 'dart:async';
import 'package:flutter/widgets.dart';
import '../../data/services/analytics_service.dart';
import '../../data/services/posthog_analytics_service.dart';

/// Reports a screen view to both analytics tools on every navigation.
///
/// Flutter draws the whole app inside one native screen (MainActivity on
/// Android, FlutterViewController on iOS). Without this observer, Firebase
/// Analytics only ever sees that single container and PostHog sees no screens
/// at all. This reads the fixed name set on each route in
/// lib/core/router/router.dart and forwards it to:
///   - Firebase Analytics (the Google Analytics dashboard) via [AnalyticsService]
///   - PostHog (the product dashboard) via [PostHogAnalyticsService]
///
/// Routes are named with fixed labels (Home, Chat, Agent Thread and so on), so a
/// dynamic path value such as a chat session id never leaks in and never
/// inflates the screen list with one entry per id.
class AppRouteObserver extends NavigatorObserver {
  AppRouteObserver({required PostHogAnalyticsService postHogAnalyticsService})
      : _postHog = postHogAnalyticsService;

  final PostHogAnalyticsService _postHog;

  @override
  void didPush(Route<dynamic> route, Route<dynamic>? previousRoute) {
    super.didPush(route, previousRoute);
    _report(route);
  }

  @override
  void didReplace({Route<dynamic>? newRoute, Route<dynamic>? oldRoute}) {
    super.didReplace(newRoute: newRoute, oldRoute: oldRoute);
    _report(newRoute);
  }

  @override
  void didPop(Route<dynamic> route, Route<dynamic>? previousRoute) {
    super.didPop(route, previousRoute);
    // Popping returns the user to the screen underneath, 
    // so that screen is the one now in view.
    _report(previousRoute);
  }

  void _report(Route<dynamic>? route) {
    final screenName = route?.settings.name;
    // Dialogs, bottom sheets and other unnamed routes carry no screen name.
    // Skip them so they never show up as blank entries in either dashboard.
    if (screenName == null || screenName.isEmpty) return;

    unawaited(AnalyticsService.logScreenView(screenName));
    unawaited(_postHog.screenView(screenName));
  }
}
