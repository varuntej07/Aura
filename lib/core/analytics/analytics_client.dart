/// Analytics surface the app depends on instead of a concrete SDK.
///
/// Mobile implements this with the posthog_flutter plugin
/// ([PostHogAnalyticsService]). Keeping the interface (rather than calling the
/// plugin directly) lets tests stub analytics and keeps the funnel contract in
/// funnel_events.dart independent of any one SDK.
abstract class AnalyticsClient {
  Future<void> trackEvent(String event, {Map<String, Object>? properties});

  Future<void> identifyUser(String uid, {Map<String, Object>? traits});

  Future<void> screenView(String screenName);

  Future<void> reset();
}
