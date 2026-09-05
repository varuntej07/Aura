import 'dart:async';

import '../../core/base/safe_change_notifier.dart';
import '../../core/errors/app_exception.dart';
import '../../core/errors/error_handler.dart';
import '../../core/logging/app_logger.dart';
import '../../data/models/user_model.dart';
import '../../data/repositories/auth_repository.dart';
import '../../data/services/backend_api_service.dart';
import '../../data/services/notification_service.dart';
import '../../data/services/store_purchase_service.dart';
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
  // Nullable for the same reason, and null on every non-iOS surface where
  // StoreKit is not the seller.
  final StorePurchaseService? _storePurchaseService;
  final AnalyticsClient _postHogAnalyticsService;
  StreamSubscription<UserModel?>? _authSubscription;
  StreamSubscription<EntitlementUpdatedPayload>? _entitlementUpdatedSub;

  AuthViewModel({
    required AuthRepository authRepository,
    required NotificationService notificationService,
    required BackendApiService backendApiService,
    SubscriptionService? subscriptionService,
    StorePurchaseService? storePurchaseService,
    required AnalyticsClient postHogAnalyticsService,
  }) : _authRepository = authRepository,
       _notificationService = notificationService,
       _backendApiService = backendApiService,
       _subscriptionService = subscriptionService,
       _storePurchaseService = storePurchaseService,
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
  bool _passwordResetInFlight = false;
  String? _passwordResetNotice;

  ViewState get state => _state;
  UserModel? get user => _user;
  AppException? get error => _error;
  bool get isAuthenticated => _user != null;
  bool get passwordResetInFlight => _passwordResetInFlight;
  String? get passwordResetNotice => _passwordResetNotice;
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
          // Attach the StoreKit listener as soon as there is an account to
          // attribute a purchase to. It must run at launch and not only when
          // the paywall opens: a purchase that completed while the app was
          // being killed is replayed on this stream, and dropping it would
          // take someone's money without granting access.
          unawaited(_storePurchaseService?.initialize() ?? Future.value());
          unawaited(_postHogAnalyticsService.identifyUser(user.uid));
        } else {
          _notificationService.clearUser();
        }
        final nextState = user != null ? ViewState.loaded : ViewState.idle;
        AppLogger.info('Auth state -> $nextState', tag: 'AuthVM');
        _setState(nextState);
      },
      onError: (Object e, StackTrace st) {
        ErrorHandler.handle(e, st);
        _error = AppException.unexpected(
          "Something went wrong. Try again in a moment.",
          error: e,
        );
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
          AppLogger.info(
            'signInWithGoogle: success uid=${user.uid}',
            tag: 'AuthVM',
          );
          _user = user;
          _error = null;
          ErrorHandler.logBreadcrumb(
            'user_signed_in',
            metadata: {'uid': user.uid},
          );
          _setState(ViewState.loaded);
        },
        failure: (error) {
          // User backed out of the Google account picker — that's a normal
          // choice, not an error. Quietly return to the login screen instead of
          // flashing a red error banner at them.
          if (error.code == ErrorCode.authCancelled) {
            AppLogger.info(
              'signInWithGoogle: cancelled by user',
              tag: 'AuthVM',
            );
            _error = null;
            _setState(ViewState.idle);
            return;
          }
          AppLogger.error(
            'signInWithGoogle: failed',
            error: error,
            tag: 'AuthVM',
          );
          _error = error;
          _setState(ViewState.error);
        },
      );
    } catch (e, st) {
      ErrorHandler.handle(e, st);
      _error = AppException.unexpected(
        "Something went wrong. Try again in a moment.",
        error: e,
      );
      _setState(ViewState.error);
    }
  }

  Future<void> signInWithApple() async {
    AppLogger.info('signInWithApple: starting', tag: 'AuthVM');
    _setState(ViewState.loading);
    try {
      final result = await _authRepository.signInWithApple();
      result.when(
        success: (user) {
          AppLogger.info(
            'signInWithApple: success uid=${user.uid}',
            tag: 'AuthVM',
          );
          _user = user;
          _error = null;
          ErrorHandler.logBreadcrumb(
            'user_signed_in_apple',
            metadata: {'uid': user.uid},
          );
          _setState(ViewState.loaded);
        },
        failure: (error) {
          if (error.code == ErrorCode.authCancelled) {
            AppLogger.info('signInWithApple: cancelled by user', tag: 'AuthVM');
            _error = null;
            _setState(ViewState.idle);
            return;
          }
          AppLogger.error(
            'signInWithApple: failed',
            error: error,
            tag: 'AuthVM',
          );
          _error = error;
          _setState(ViewState.error);
        },
      );
    } catch (e, st) {
      ErrorHandler.handle(e, st);
      _error = AppException.unexpected(
        "Something went wrong. Try again in a moment.",
        error: e,
      );
      _setState(ViewState.error);
    }
  }

  /// Requests a password reset link.
  ///
  /// Deliberately does NOT touch [_state]: the router derives `isReady` from it
  /// (`state != idle && state != loading`), and the login screen swaps itself
  /// for a full-screen "Signing in…" loader on [ViewState.loading]. A reset
  /// request is a side channel that neither authenticates nor navigates, so it
  /// carries its own in-flight and notice fields instead.
  Future<void> sendPasswordResetEmail(String email) async {
    AppLogger.info('sendPasswordResetEmail: starting', tag: 'AuthVM');
    _passwordResetInFlight = true;
    _passwordResetNotice = null;
    _error = null;
    safeNotifyListeners();
    try {
      final result = await _authRepository.sendPasswordResetEmail(email);
      result.when(
        success: (_) {
          AppLogger.info('sendPasswordResetEmail: request accepted',
              tag: 'AuthVM');
          // One message whether or not an account exists — the service maps
          // user-not-found to success on purpose, so this branch must stay the
          // only place a confirmation is produced.
          _passwordResetNotice =
              "If there's an Aura account for $email, a reset link is on its "
              "way. Check your inbox, and your spam folder just in case.";
        },
        failure: (error) {
          AppLogger.error(
            'sendPasswordResetEmail: failed',
            error: error,
            tag: 'AuthVM',
          );
          _error = error;
        },
      );
    } catch (e, st) {
      ErrorHandler.handle(e, st);
      _error = AppException.unexpected(
        'Something went wrong. Try again in a moment.',
        error: e,
      );
    } finally {
      _passwordResetInFlight = false;
      safeNotifyListeners();
    }
  }

  /// Clears the reset notice so the form does not reappear already-confirmed.
  void clearPasswordResetNotice() {
    if (_passwordResetNotice == null) return;
    _passwordResetNotice = null;
    safeNotifyListeners();
  }

  Future<void> signInWithEmail(String email, String password) async {
    AppLogger.info('signInWithEmail: starting', tag: 'AuthVM');
    _setState(ViewState.loading);
    try {
      final result = await _authRepository.signInWithEmail(email, password);
      result.when(
        success: (user) {
          AppLogger.info(
            'signInWithEmail: success uid=${user.uid}',
            tag: 'AuthVM',
          );
          _user = user;
          _error = null;
          ErrorHandler.logBreadcrumb(
            'user_signed_in_email',
            metadata: {'uid': user.uid},
          );
          _setState(ViewState.loaded);
        },
        failure: (error) {
          AppLogger.error(
            'signInWithEmail: failed',
            error: error,
            tag: 'AuthVM',
          );
          _error = error;
          _setState(ViewState.error);
        },
      );
    } catch (e, st) {
      ErrorHandler.handle(e, st);
      _error = AppException.unexpected(
        "Something went wrong. Try again in a moment.",
        error: e,
      );
      _setState(ViewState.error);
    }
  }

  Future<void> createAccountWithEmail(
    String email,
    String password,
    String name,
  ) async {
    AppLogger.info('createAccountWithEmail: starting', tag: 'AuthVM');
    _setState(ViewState.loading);
    try {
      final result = await _authRepository.createAccountWithEmail(
        email,
        password,
        name,
      );
      result.when(
        success: (user) {
          AppLogger.info(
            'createAccountWithEmail: success uid=${user.uid}',
            tag: 'AuthVM',
          );
          _user = user;
          _error = null;
          ErrorHandler.logBreadcrumb(
            'user_created_email',
            metadata: {'uid': user.uid},
          );
          _setState(ViewState.loaded);
        },
        failure: (error) {
          AppLogger.error(
            'createAccountWithEmail: failed',
            error: error,
            tag: 'AuthVM',
          );
          _error = error;
          _setState(ViewState.error);
        },
      );
    } catch (e, st) {
      ErrorHandler.handle(e, st);
      _error = AppException.unexpected(
        "Something went wrong. Try again in a moment.",
        error: e,
      );
      _setState(ViewState.error);
    }
  }

  /// Called after `OnboardingRepository.saveOnboardingResult` succeeds.
  /// Updates the in-memory user so the router redirect fires immediately
  /// without waiting for the Firestore stream to re-emit. [displayName] carries
  /// the name the user chose during onboarding so Buddy greets them with it from
  /// the very first message, rather than the stale provider name (the auth stream
  /// only re-emits on auth changes, not doc writes).
  void markOnboardingComplete({
    required bool auraConsentGranted,
    String? displayName,
  }) {
    if (_user == null) return;
    _user = _user!.copyWith(
      onboardingComplete: true,
      auraConsentGranted: auraConsentGranted,
      displayName: displayName,
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
    final result = await _backendApiService.revokeAuraMemory();
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
    await _notificationService.deactivateForSignOut();
    await _authRepository.signOut();
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
