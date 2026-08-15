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

  /// Seeds the user this VM writes through.
  ///
  /// Deliberately silent. The screen calls this synchronously from initState and
  /// then reads [settings] on its very first build, so a notify here would only
  /// schedule a redundant rebuild one frame later — which is exactly the flicker
  /// of the page painting defaults and then correcting itself. Callers that need
  /// the UI to react to a *changed* user go through [_updateSettings].
  void loadUser(UserModel user) {
    _user = user;
    _state = ViewState.loaded;
  }

  Future<void> toggleWakeWord(bool enabled) async {
    if (_user == null) return;
    await _updateSettings(_user!.settings.copyWith(wakeWordEnabled: enabled));
  }

  Future<void> toggleTts(bool enabled) async {
    if (_user == null) return;
    await _updateSettings(_user!.settings.copyWith(ttsEnabled: enabled));
  }

  /// Persists the voice Buddy speaks in. Takes effect at the next session start,
  /// not mid-call: the TTS pipeline is constructed once when the worker joins the
  /// room. The picker says so rather than leaving the user to notice.
  ///
  /// Entitlement is deliberately not checked here. The lock in the picker is
  /// cosmetic; `voice_catalog.resolve_voice` is the real boundary and re-checks
  /// tier every session, so a lapse reverts the voice without needing the client
  /// to have written anything different.
  Future<void> selectVoice(String voiceSlug) async {
    if (_user == null) return;
    if (_user!.settings.ttsVoiceId == voiceSlug) return;
    await _updateSettings(_user!.settings.copyWith(ttsVoiceId: voiceSlug));
  }

  /// Persists the default sound alarms ring with.
  ///
  /// Takes effect on the next sync rather than instantly: the device holds a
  /// schedule the OS already registered, and the tone travels with it. Alarms
  /// that carry their own tone (Buddy set one because the user asked for it) are
  /// unaffected, because the override wins server-side.
  Future<void> selectAlarmTone(String slug) async {
    if (_user == null) return;
    if (_user!.settings.alarmTone == slug) return;
    await _updateSettings(_user!.settings.copyWith(alarmTone: slug));
  }

  Future<void> _updateSettings(UserSettings newSettings) async {
    if (_user == null) return;

    // No loading state. The write is optimistic, so the switch is already in its
    // new position; a `loading` state here used to blank the entire Settings page
    // behind a full-screen spinner until Firestore acked, then rebuild the list
    // from scratch and lose the scroll position. One notify, not two.
    _user = _user!.copyWith(settings: newSettings);
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
