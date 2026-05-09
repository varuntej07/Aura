import 'dart:async';

import '../../core/base/safe_change_notifier.dart';
import '../../core/errors/app_exception.dart';
import '../../core/errors/error_handler.dart';
import '../../core/logging/app_logger.dart';
import '../../data/models/user_model.dart';
import '../../data/repositories/auth_repository.dart';
import '../../data/services/notification_service.dart';
import 'view_state.dart';

export 'view_state.dart';

class AuthViewModel extends SafeChangeNotifier {
  final AuthRepository _authRepository;
  final NotificationService _notificationService;
  StreamSubscription<UserModel?>? _authSubscription;

  AuthViewModel({
    required AuthRepository authRepository,
    required NotificationService notificationService,
  })  : _authRepository = authRepository,
        _notificationService = notificationService;

  ViewState _state = ViewState.idle;
  UserModel? _user;
  AppException? _error;

  ViewState get state => _state;
  UserModel? get user => _user;
  AppException? get error => _error;
  bool get isAuthenticated => _user != null;

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
        _error = AppException.unexpected(e.toString());
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
      _error = AppException.unexpected(e.toString());
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
      _error = AppException.unexpected(e.toString());
      _setState(ViewState.error);
    }
  }

  Future<void> signOut() async {
    _user = null;
    _error = null;
    ErrorHandler.logBreadcrumb('user_signed_out');
    _setState(ViewState.idle);
    unawaited(_authRepository.signOut());
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
