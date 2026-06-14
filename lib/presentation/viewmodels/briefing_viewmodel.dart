import 'dart:async';

import '../../core/analytics/funnel_events.dart';
import '../../core/base/safe_change_notifier.dart';
import '../../data/models/daily_briefing.dart';
import '../../data/services/backend_api_service.dart';
import '../../data/services/posthog_analytics_service.dart';
import 'view_state.dart';

/// Backs the briefing screen. Fetches today's briefing from the backend (the same
/// digest the BriefingAgent wrote from the ranked content pool) and exposes it for
/// display. Fires the `briefing_opened` funnel step once on a successful load,
/// whether the screen was opened from the push tap or the drawer.
class BriefingViewModel extends SafeChangeNotifier {
  final BackendApiService _backendApiService;
  final PostHogAnalyticsService _postHogAnalyticsService;

  BriefingViewModel({
    required BackendApiService backendApiService,
    required PostHogAnalyticsService postHogAnalyticsService,
  })  : _backendApiService = backendApiService,
        _postHogAnalyticsService = postHogAnalyticsService;

  ViewState _state = ViewState.idle;
  DailyBriefing? _briefing;
  bool _isWorldSnapshot = false;
  bool _fetchingWorld = false;
  String? _worldError;

  ViewState get state => _state;
  DailyBriefing? get briefing => _briefing;

  /// True when the shown briefing is the on-demand world snapshot (vs. the scheduled
  /// personalized digest), so the screen can label it accordingly.
  bool get isWorldSnapshot => _isWorldSnapshot;

  /// True while a "Catch me up on the world" fetch is in flight (drives the button
  /// spinner and the refresh icon spinner).
  bool get fetchingWorld => _fetchingWorld;

  /// Friendly, casual error from the last failed world fetch, or null. The mic-orb
  /// doctrine: every wait ends in a visible message that points at the next action.
  String? get worldError => _worldError;

  /// True when the fetch finished and there is no briefing ready (empty state).
  bool get isEmpty => _state == ViewState.loaded && _briefing == null;

  Future<void> load() async {
    _state = ViewState.loading;
    safeNotifyListeners();

    final briefing = await _backendApiService.fetchTodayBriefing();
    _briefing = briefing;
    _state = ViewState.loaded;
    safeNotifyListeners();

    if (briefing != null) {
      unawaited(_postHogAnalyticsService.trackEvent(
        FunnelEvents.briefingOpened,
        properties: {
          FunnelEvents.propNotificationOrigin: FunnelEvents.originBriefing,
        },
      ));
    }
  }

  /// Fetches the on-demand "Catch me up on the world" snapshot and shows it in place
  /// of the empty state. [refresh] forces a server regenerate (the refresh icon);
  /// otherwise a warm region cache is served. On failure, sets [worldError] so the UI
  /// shows a casual retry message rather than hanging. Fires `world_briefing_fetched`
  /// once on a successful load (the analogue of `briefing_opened`).
  Future<void> fetchWorldNow({bool refresh = false}) async {
    if (_fetchingWorld) return;
    _fetchingWorld = true;
    _worldError = null;
    safeNotifyListeners();

    final briefing = await _backendApiService.fetchWorldBriefing(refresh: refresh);

    _fetchingWorld = false;
    if (briefing != null) {
      _briefing = briefing;
      _isWorldSnapshot = true;
      _state = ViewState.loaded;
      unawaited(_postHogAnalyticsService.trackEvent(
        FunnelEvents.worldBriefingFetched,
        properties: {
          FunnelEvents.propNotificationOrigin: FunnelEvents.originBriefing,
        },
      ));
    } else {
      _worldError = "Couldn't reach the world right now. Give it another go in a sec.";
    }
    safeNotifyListeners();
  }
}
