import '../../core/base/safe_change_notifier.dart';
import '../../core/constants/app_constants.dart';
import '../../core/errors/app_exception.dart';
import '../../core/errors/error_handler.dart';
import '../../core/logging/app_logger.dart';
import '../../data/models/user_model.dart';
import '../../data/services/app_feedback_service.dart';
import '../../data/services/firestore_service.dart';
import 'view_state.dart';

export 'view_state.dart';

class SettingsViewModel extends SafeChangeNotifier {
  final FirestoreService _firestoreService;
  final AppFeedbackService _appFeedbackService;

  SettingsViewModel({
    required FirestoreService firestoreService,
    required AppFeedbackService appFeedbackService,
  })  : _firestoreService = firestoreService,
        _appFeedbackService = appFeedbackService;

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

  /// Beta feedback capture. Delegates to [AppFeedbackService] so typed feedback
  /// and voice-orb ratings share one write path and one root collection.
  Future<String?> submitFeedback({
    required String text,
    required String category,
  }) async {
    final user = _user;
    if (user == null) {
      return "You're signed out. Sign back in to send feedback.";
    }
    return _appFeedbackService.submit(
      uid: user.uid,
      category: category,
      text: text,
    );
  }

  void clearError() {
    _error = null;
    safeNotifyListeners();
  }
}
