import 'dart:async';

import '../../core/base/safe_change_notifier.dart';
import '../../core/errors/app_exception.dart';
import '../../core/errors/error_handler.dart';
import '../../core/logging/app_logger.dart';
import '../../data/models/user_model.dart';
import '../../data/repositories/auth_repository.dart';
import '../../data/services/backend_api_service.dart';
import '../../data/services/notification_service.dart';
import '../../data/services/posthog_analytics_service.dart';
import 'view_state.dart';

export 'view_state.dart';

class AuthViewModel extends SafeChangeNotifier {
  final AuthRepository _authRepository;
  final NotificationService _notificationService;
  final BackendApiService _backendApiService;
  final PostHogAnalyticsService _postHogAnalyticsService;
  StreamSubscription<UserModel?>? _authSubscription;

  AuthViewModel({
    required AuthRepository authRepository,
    required NotificationService notificationService,
    required BackendApiService backendApiService,
    required PostHogAnalyticsService postHogAnalyticsService,
  })  : _authRepository = authRepository,
        _notificationService = notificationService,
        _backendApiService = backendApiService,
        _postHogAnalyticsService = postHogAnalyticsService;

  ViewState _state = ViewState.idle;
  UserModel? _user;
  AppException? _error;
  bool _justCompletedOnboarding = false;

  ViewState get state => _state;
  UserModel? get user => _user;
  AppException? get error => _error;
  bool get isAuthenticated => _user != null;
  bool get needsOnboarding => _user != null && !_user!.onboardingComplete;

  /// True immediately after onboarding completes. Used to show the guided
  /// first-message prompt in the chat panel. Consumed once by the UI.
  bool get justCompletedOnboarding => _justCompletedOnboarding;

  void consumeFirstSessionPrompt() {
    _justCompletedOnboarding = false;
    safeNotifyListeners();
  }

  void _setState(ViewState s) {
    _state = s;
    safeNotifyListeners();
  }

  // Subscribes to the Firebase auth state stream.
  // Fires immediately with the current auth state, then again on every change
  // (sign-in, sign-out, token revocation). The router re-evaluates its redirect
  // on every notifyListeners call, so navigation is always in sync.
  Future<void> initialize() async {
    _setState(ViewState.loading);
    _authSubscription = _authRepository.userModelStream.listen(
      (user) {
        AppLogger.info(
          'Auth stream emitted: ${user != null ? 'user=${user.uid}' : 'null (logged out)'}',
          tag: 'AuthVM',
        );
        _user = user;
        _error = null;
        if (user != null) {
          ErrorHandler.setUser(user.uid);
          unawaited(_notificationService.initialize(user.uid));
          unawaited(_postHogAnalyticsService.identifyUser(user.uid));
        }
        final nextState = user != null ? ViewState.loaded : ViewState.idle;
        AppLogger.info(
          'Auth state -> $nextState',
          tag: 'AuthVM',
        );
        _setState(nextState);
      },
      onError: (Object e, StackTrace st) {
        ErrorHandler.handle(e, st);
        _error = AppException.unexpected("Something went wrong. Try again in a moment.", error: e);
        _setState(ViewState.error);
        AppLogger.error('Auth stream error', error: e, tag: 'AuthVM');
      },
    );
  }

  Future<void> signInWithGoogle() async {
    AppLogger.info('signInWithGoogle: starting', tag: 'AuthVM');
    _setState(ViewState.loading);
    try {
      final result = await _authRepository.signInWithGoogle();
      result.when(
        success: (user) {
          AppLogger.info('signInWithGoogle: success uid=${user.uid}', tag: 'AuthVM');
          _user = user;
          _error = null;
          ErrorHandler.logBreadcrumb('user_signed_in',
              metadata: {'uid': user.uid});
          _setState(ViewState.loaded);
        },
        failure: (error) {
          AppLogger.error('signInWithGoogle: failed', error: error, tag: 'AuthVM');
          _error = error;
          _setState(ViewState.error);
        },
      );
    } catch (e, st) {
      ErrorHandler.handle(e, st);
      _error = AppException.unexpected("Something went wrong. Try again in a moment.", error: e);
      _setState(ViewState.error);
    }
  }

  Future<void> signInWithEmail(String email, String password) async {
    AppLogger.info('signInWithEmail: starting', tag: 'AuthVM');
    _setState(ViewState.loading);
    try {
      final result = await _authRepository.signInWithEmail(email, password);
      result.when(
        success: (user) {
          AppLogger.info('signInWithEmail: success uid=${user.uid}', tag: 'AuthVM');
          _user = user;
          _error = null;
          ErrorHandler.logBreadcrumb('user_signed_in_email',
              metadata: {'uid': user.uid});
          _setState(ViewState.loaded);
        },
        failure: (error) {
          AppLogger.error('signInWithEmail: failed', error: error, tag: 'AuthVM');
          _error = error;
          _setState(ViewState.error);
        },
      );
    } catch (e, st) {
      ErrorHandler.handle(e, st);
      _error = AppException.unexpected("Something went wrong. Try again in a moment.", error: e);
      _setState(ViewState.error);
    }
  }

  Future<void> createAccountWithEmail(
      String email, String password, String name) async {
    AppLogger.info('createAccountWithEmail: starting', tag: 'AuthVM');
    _setState(ViewState.loading);
    try {
      final result =
          await _authRepository.createAccountWithEmail(email, password, name);
      result.when(
        success: (user) {
          AppLogger.info('createAccountWithEmail: success uid=${user.uid}',
              tag: 'AuthVM');
          _user = user;
          _error = null;
          ErrorHandler.logBreadcrumb('user_created_email',
              metadata: {'uid': user.uid});
          _setState(ViewState.loaded);
        },
        failure: (error) {
          AppLogger.error('createAccountWithEmail: failed',
              error: error, tag: 'AuthVM');
          _error = error;
          _setState(ViewState.error);
        },
      );
    } catch (e, st) {
      ErrorHandler.handle(e, st);
      _error = AppException.unexpected(
          "Something went wrong. Try again in a moment.", error: e);
      _setState(ViewState.error);
    }
  }

  /// Called after `OnboardingRepository.saveOnboardingResult` succeeds.
  /// Updates the in-memory user so the router redirect fires immediately
  /// without waiting for the Firestore stream to re-emit.
  void markOnboardingComplete({required bool auraConsentGranted}) {
    if (_user == null) return;
    _user = _user!.copyWith(
      onboardingComplete: true,
      auraConsentGranted: auraConsentGranted,
    );
    _justCompletedOnboarding = true;
    safeNotifyListeners();
  }

  Future<void> signOut() async {
    _user = null;
    _error = null;
    ErrorHandler.logBreadcrumb('user_signed_out');
    unawaited(_postHogAnalyticsService.reset());
    _setState(ViewState.idle);
    unawaited(_authRepository.signOut());
  }

  /// Permanently deletes the account. Calls the backend to wipe all Firestore
  /// data and the Firebase Auth user, then signs out locally.
  /// Returns null on success, or an error message string on failure.
  Future<String?> deleteAccount() async {
    _setState(ViewState.loading);
    final result = await _backendApiService.deleteAccount();
    return result.when(
      success: (_) {
        _user = null;
        _error = null;
        _setState(ViewState.idle);
        unawaited(_authRepository.signOut());
        return null;
      },
      failure: (error) {
        AppLogger.error('deleteAccount failed', error: error, tag: 'AuthVM');
        _setState(ViewState.loaded);
        return 'Something went wrong. Try again in a moment.';
      },
    );
  }

  void clearError() {
    _error = null;
    safeNotifyListeners();
  }

  @override
  void dispose() {
    _authSubscription?.cancel();
    super.dispose();
  }
}
