import 'dart:async';
import 'dart:io';

import '../../core/base/safe_change_notifier.dart';
import '../../core/constants/app_constants.dart';
import '../../core/errors/app_exception.dart';
import '../../core/errors/error_handler.dart';
import '../../core/logging/app_logger.dart';
import '../../data/models/user_model.dart';
import '../../data/services/firestore_service.dart';
import '../../data/services/posthog_analytics_service.dart';
import 'view_state.dart';

export 'view_state.dart';

class SettingsViewModel extends SafeChangeNotifier {
  final FirestoreService _firestoreService;
  final PostHogAnalyticsService _postHogAnalyticsService;

  SettingsViewModel({
    required FirestoreService firestoreService,
    required PostHogAnalyticsService postHogAnalyticsService,
  })  : _firestoreService = firestoreService,
        _postHogAnalyticsService = postHogAnalyticsService;

  ViewState _state = ViewState.idle;
  UserModel? _user;
  AppException? _error;

  ViewState get state => _state;
  UserModel? get user => _user;
  UserSettings? get settings => _user?.settings;
  AppException? get error => _error;

  void _setState(ViewState s) {
    _state = s;
    safeNotifyListeners();
  }

  void loadUser(UserModel user) {
    _user = user;
    _setState(ViewState.loaded);
  }

  Future<void> toggleWakeWord(bool enabled) async {
    if (_user == null) return;
    await _updateSettings(_user!.settings.copyWith(wakeWordEnabled: enabled));
  }

  Future<void> toggleTts(bool enabled) async {
    if (_user == null) return;
    await _updateSettings(_user!.settings.copyWith(ttsEnabled: enabled));
  }

  Future<void> _updateSettings(UserSettings newSettings) async {
    if (_user == null) return;
    _setState(ViewState.loading);

    final optimisticUser = _user!.copyWith(settings: newSettings);
    _user = optimisticUser;
    safeNotifyListeners();

    try {
      final result = await _firestoreService.updateDocument(
        AppConstants.usersCollection,
        _user!.uid,
        {'settings': newSettings.toJson()},
      );
      result.when(
        success: (_) {
          AppLogger.info('Settings updated', tag: 'SettingsVM');
          _setState(ViewState.loaded);
        },
        failure: (error) {
          _error = error;
          _setState(ViewState.error);
          AppLogger.error('Settings update failed', error: error, tag: 'SettingsVM');
        },
      );
    } catch (e, st) {
      ErrorHandler.handle(e, st);
      _error = AppException.unexpected("Something went wrong. Try again in a moment.", error: e);
      _setState(ViewState.error);
    }
  }

  /// Beta feedback capture. Writes one document per submission to the
  /// root `app_feedback/{timestamp}_{uid}` collection (each doc carries a
  /// `uid` field) so every report is reviewable in one place instead of being
  /// buried in each user's subcollection. Fires a PostHog `feedback_submitted`
  /// event tagged with the category.
  Future<String?> submitFeedback({
    required String text,
    required String category,
  }) async {
    final user = _user;
    if (user == null) {
      return "You're signed out. Sign back in to send feedback.";
    }

    unawaited(_postHogAnalyticsService.trackEvent(
      'feedback_submitted',
      properties: {'category': category},
    ));

    // Timestamp prefix keeps the console's lexicographic doc ordering
    // chronological; the uid suffix prevents collisions across users.
    final timestamp = DateTime.now().toUtc().millisecondsSinceEpoch.toString();
    final docId = '${timestamp}_${user.uid}';
    final result = await _firestoreService.setDocument<Map<String, dynamic>>(
      AppConstants.appFeedbackCollection,
      docId,
      {
        'uid': user.uid,
        'text': text.trim(),
        'category': category,
        'created_at': DateTime.now().toUtc().toIso8601String(),
        'app_version': '1.0.0',
        'platform': Platform.isIOS ? 'ios' : 'android',
      },
      (json) => json,
      merge: false,
    );

    return result.when(
      success: (_) {
        AppLogger.info('Feedback submitted ($category)', tag: 'SettingsVM');
        return null;
      },
      failure: (error) {
        AppLogger.error('Feedback submit failed', error: error, tag: 'SettingsVM');
        return "Couldn't send that just now. Check your connection and try again.";
      },
    );
  }

  void clearError() {
    _error = null;
    safeNotifyListeners();
  }
}
