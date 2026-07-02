/// Analytics surface shared by every platform build.
///
/// Mobile implements this with the posthog_flutter plugin
/// ([PostHogAnalyticsService]); Windows desktop implements it with a direct
/// HTTP capture client ([PostHogHttpAnalytics]) because posthog_flutter has no
/// Windows implementation. Same event names flow through both, so the funnel
/// contract in funnel_events.dart holds across platforms.
abstract class AnalyticsClient {
  Future<void> trackEvent(String event, {Map<String, Object>? properties});

  Future<void> identifyUser(String uid, {Map<String, Object>? traits});

  Future<void> screenView(String screenName);

  Future<void> reset();
}
