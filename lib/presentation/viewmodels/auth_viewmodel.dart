import 'dart:async';

import '../../core/base/safe_change_notifier.dart';
import '../../core/errors/app_exception.dart';
import '../../core/errors/error_handler.dart';
import '../../core/logging/app_logger.dart';
import '../../data/models/user_model.dart';
import '../../data/repositories/auth_repository.dart';
import '../../data/services/backend_api_service.dart';
import '../../data/services/notification_service.dart';
import '../../data/services/subscription_service.dart';
import '../../core/analytics/analytics_client.dart';
import 'view_state.dart';

export 'view_state.dart';

class AuthViewModel extends SafeChangeNotifier {
  final AuthRepository _authRepository;
  final NotificationService _notificationService;
  final BackendApiService _backendApiService;
  // Nullable: the desktop DI graph excludes the subscription service entirely
  // (no paywall/entitlement surface there), so it never supplies this.
  final SubscriptionService? _subscriptionService;
  final AnalyticsClient _postHogAnalyticsService;
  StreamSubscription<UserModel?>? _authSubscription;
  StreamSubscription<EntitlementUpdatedPayload>? _entitlementUpdatedSub;

  AuthViewModel({
    required AuthRepository authRepository,
    required NotificationService notificationService,
    required BackendApiService backendApiService,
    SubscriptionService? subscriptionService,
    required AnalyticsClient postHogAnalyticsService,
  })  : _authRepository = authRepository,
        _notificationService = notificationService,
        _backendApiService = backendApiService,
        _subscriptionService = subscriptionService,
        _postHogAnalyticsService = postHogAnalyticsService {
    // The billing webhook's sync push: refetch entitlement so this device
    // unlocks (or downgrades) within seconds of the payment event landing.
    _entitlementUpdatedSub = _notificationService.entitlementUpdatedStream
        .listen((_) => _subscriptionService?.refreshEntitlement());
  }

  ViewState _state = ViewState.idle;
  UserModel? _user;
  AppException? _error;
  bool _justCompletedOnboarding = false;

  ViewState get state => _state;
  UserModel? get user => _user;
  AppException? get error => _error;
  bool get isAuthenticated => _user != null;
  bool get needsOnboarding => _user != null && !_user!.onboardingComplete;

  /// True only when the user has explicitly granted Aura memory. Drives the
  /// in-app toggle and the "turn it on" prompt: absent consent (legacy accounts
  /// predating the consent screen) and an explicit `false` (declined / under-18 /
  /// later withdrawn) both read as off, so both surface the prompt to turn it on.
  bool get auraMemoryEnabled => _user?.auraConsentGranted == true;

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
          if (_subscriptionService != null) {
            unawaited(_subscriptionService.refreshEntitlement());
          }
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
          // User backed out of the Google account picker — that's a normal
          // choice, not an error. Quietly return to the login screen instead of
          // flashing a red error banner at them.
          if (error.code == ErrorCode.authCancelled) {
            AppLogger.info('signInWithGoogle: cancelled by user', tag: 'AuthVM');
            _error = null;
            _setState(ViewState.idle);
            return;
          }
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

  /// Withdraws Aura memory consent (the GDPR right to withdraw, as easy as it was
  /// to grant). Writes `aura_consent_granted: false` to the user doc and updates
  /// the in-memory model immediately, so every reader stops within one turn
  /// without waiting for the auth stream (which only re-emits on auth changes,
  /// not doc writes). Granting goes the other way, through the age-gated consent
  /// screen, never here. Returns true on success.
  Future<bool> revokeAuraMemory() async {
    final uid = _user?.uid;
    if (uid == null) return false;
    final result = await _authRepository.setAuraConsentGranted(uid, false);
    return result.when(
      success: (_) {
        _user = _user!.copyWith(auraConsentGranted: false);
        safeNotifyListeners();
        return true;
      },
      failure: (error) {
        AppLogger.error('revokeAuraMemory failed', error: error, tag: 'AuthVM');
        _error = error;
        safeNotifyListeners();
        return false;
      },
    );
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
    _entitlementUpdatedSub?.cancel();
    super.dispose();
  }
}
