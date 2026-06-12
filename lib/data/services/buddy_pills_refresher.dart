import 'dart:async';

import '../../core/logging/app_logger.dart';
import 'backend_api_service.dart';

/// Coordinates "regenerate the Buddy chat suggestion pills after a real session".
///
/// The activity flag is set whenever the user does something worth grounding new
/// pills on — sends a chat message OR starts a voice session. When the app is
/// backgrounded, [refreshIfActivity] fires one fire-and-forget regenerate so
/// fresh pills are waiting on the next visit, then clears the flag.
///
/// Why this shape: a session with no activity costs nothing (no flag → no call),
/// rapid app-switching can't spam the backend (the flag clears on the first
/// background), and voice is covered for free because the flag is
/// modality-agnostic — it never inspects what kind of session happened.
///
/// The flag is app-session lifecycle state (like an init/connection flag), not
/// per-request transient state, so it's allowed to live on the service instance.
class BuddyPillsRefresher {
  final BackendApiService _backendApiService;
  bool _didActivityThisSession = false;

  BuddyPillsRefresher({required BackendApiService backendApiService})
      : _backendApiService = backendApiService;

  /// Record that the user did something this session (sent a chat message or
  /// started a voice session). Cheap and idempotent.
  void markActivity() {
    _didActivityThisSession = true;
  }

  /// Fire one Buddy-pills regenerate if there was activity since the last
  /// refresh. Call this when the app goes to the background. Never throws — a
  /// failed refresh just leaves the previous pills in place.
  Future<void> refreshIfActivity(String? uid) async {
    if (!_didActivityThisSession || uid == null || uid.isEmpty) return;
    // Clear first so a quick background→foreground→background bounce doesn't
    // fire a second redundant call before this one returns.
    _didActivityThisSession = false;

    final result = await _backendApiService.refreshBuddyPills();
    result.when(
      success: (_) => AppLogger.info(
        'Buddy pills refresh requested',
        tag: 'BuddyPillsRefresher',
      ),
      failure: (e) => AppLogger.warning(
        'Buddy chat suggestion pills refresh failed (non-blocking)',
        tag: 'BuddyPillsRefresher',
        metadata: {'error': e.message},
      ),
    );
  }
}
