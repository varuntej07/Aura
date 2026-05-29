import 'package:firebase_auth/firebase_auth.dart';
import 'package:firebase_core/firebase_core.dart';
import 'package:google_sign_in/google_sign_in.dart';

import '../../core/config/environment.dart';
import '../../core/errors/app_exception.dart';
import '../../core/logging/app_logger.dart';
import '../../core/network/api_response.dart';

class FirebaseAuthService {
  final FirebaseAuth? _auth;
  final GoogleSignIn _googleSignIn;
  Future<void>? _initialization;

  FirebaseAuthService({
    FirebaseAuth? auth,
    GoogleSignIn? googleSignIn,
  }) : _auth = auth ?? _resolveAuth(),
       _googleSignIn = googleSignIn ?? GoogleSignIn.instance;

  static FirebaseAuth? _resolveAuth() {
    try {
      if (Firebase.apps.isEmpty) return null;
      return FirebaseAuth.instance;
    } catch (_) {
      return null;
    }
  }

  Stream<User?> get authStateStream =>
      _auth?.authStateChanges() ?? const Stream<User?>.empty();

  User? get currentUser => _auth?.currentUser;

  Future<void> _ensureInitialized() {
    final existing = _initialization;
    if (existing != null) return existing;

    final future = _googleSignIn
        .initialize(serverClientId: Environment.current.googleServerClientId)
        .then((_) {
          AppLogger.info(
            'Google Sign-In initialized',
            tag: 'FirebaseAuthService',
          );
        }).catchError((Object error, StackTrace stackTrace) {
          AppLogger.error(
            'Google Sign-In initialization failed',
            error: error,
            stackTrace: stackTrace,
            tag: 'FirebaseAuthService',
          );
          throw error;
        });

    _initialization = future;
    return future;
  }

  Future<String?> getIdToken({bool forceRefresh = false}) async {
    final auth = _auth;
    if (auth == null) return null;

    try {
      return await auth.currentUser?.getIdToken(forceRefresh);
    } catch (e) {
      AppLogger.error(
        'Failed to get ID token',
        error: e,
        tag: 'FirebaseAuthService',
      );
      return null;
    }
  }

  Future<Result<User>> signInWithGoogle() async {
    final auth = _auth;
    if (auth == null) {
      return Result.failure(
        AppException.unexpected(
          'Firebase authentication is not configured for this build.',
        ),
      );
    }

    try {
      await _ensureInitialized();
      final googleUser = await _googleSignIn.authenticate();
      final googleAuth = googleUser.authentication;
      final credential = GoogleAuthProvider.credential(
        idToken: googleAuth.idToken,
      );

      final userCredential = await auth.signInWithCredential(credential);
      final user = userCredential.user;
      if (user == null) {
        return Result.failure(
          AppException.authFailed(Exception('No user after sign-in')),
        );
      }

      AppLogger.info(
        'Google sign-in successful',
        tag: 'FirebaseAuthService',
        metadata: {'uid': user.uid},
      );
      return Result.success(user);
    } on GoogleSignInException catch (e, st) {
      AppLogger.error(
        'Google sign-in failed',
        error: e,
        stackTrace: st,
        tag: 'FirebaseAuthService',
      );
      final isUserCancellation = e.code == GoogleSignInExceptionCode.canceled &&
          (e.description == null ||
              e.description!.isEmpty ||
              e.description!.toLowerCase().contains('cancel'));
      if (isUserCancellation) {
        return Result.failure(AppException.authCancelled());
      }
      return Result.failure(AppException.authFailed(e, st));
    } catch (e, st) {
      AppLogger.error(
        'Google sign-in failed',
        error: e,
        stackTrace: st,
        tag: 'FirebaseAuthService',
      );
      return Result.failure(AppException.authFailed(e, st));
    }
  }

  Future<Result<String>> requestServerAuthCode(List<String> scopes) async {
    try {
      await _ensureInitialized();
      final googleUser = await _googleSignIn.authenticate();

      final currentGrant = await googleUser.authorizationClient
          .authorizationForScopes(scopes);
      if (currentGrant == null) {
        await googleUser.authorizationClient.authorizeScopes(scopes);
      }

      final serverAuth = await googleUser.authorizationClient.authorizeServer(
        scopes,
      );
      final serverAuthCode = serverAuth?.serverAuthCode ?? '';
      if (serverAuthCode.isEmpty) {
        return Result.failure(
          AppException.unexpected(
            'Google Calendar server authorization code was not returned.',
          ),
        );
      }

      return Result.success(serverAuthCode);
    } on GoogleSignInException catch (e, st) {
      AppLogger.error(
        'Google server auth code request failed',
        error: e,
        stackTrace: st,
        tag: 'FirebaseAuthService',
      );
      final isUserCancellation = e.code == GoogleSignInExceptionCode.canceled &&
          (e.description == null ||
              e.description!.isEmpty ||
              e.description!.toLowerCase().contains('cancel'));
      if (isUserCancellation) {
        return Result.failure(AppException.authCancelled());
      }
      return Result.failure(AppException.authFailed(e, st));
    } catch (e, st) {
      AppLogger.error(
        'Google server auth code request failed',
        error: e,
        stackTrace: st,
        tag: 'FirebaseAuthService',
      );
      return Result.failure(
        AppException.unexpected(
          'Unable to authorize Google Calendar access.',
          error: e,
          stackTrace: st,
        ),
      );
    }
  }

  Future<Result<User>> signInWithEmailAndPassword(
      String email, String password) async {
    final auth = _auth;
    if (auth == null) {
      return Result.failure(
          AppException.unexpected('Firebase not configured.'));
    }
    try {
      final credential = await auth.signInWithEmailAndPassword(
          email: email, password: password);
      final user = credential.user;
      if (user == null) {
        return Result.failure(
            AppException.authFailed(Exception('No user returned.')));
      }
      AppLogger.info('Email sign-in successful',
          tag: 'FirebaseAuthService', metadata: {'uid': user.uid});
      return Result.success(user);
    } on FirebaseAuthException catch (e, st) {
      AppLogger.error('Email sign-in failed',
          error: e, stackTrace: st, tag: 'FirebaseAuthService');
      return Result.failure(_mapSignInError(e, st));
    } catch (e, st) {
      return Result.failure(AppException.authFailed(e, st));
    }
  }

  Future<Result<User>> createUserWithEmailAndPassword(
      String email, String password, String name) async {
    final auth = _auth;
    if (auth == null) {
      return Result.failure(
          AppException.unexpected('Firebase not configured.'));
    }
    try {
      final credential = await auth.createUserWithEmailAndPassword(
          email: email, password: password);
      final user = credential.user;
      if (user == null) {
        return Result.failure(
            AppException.authFailed(Exception('No user returned.')));
      }
      if (name.isNotEmpty) {
        await user.updateDisplayName(name);
      }
      AppLogger.info('Email account created',
          tag: 'FirebaseAuthService', metadata: {'uid': user.uid});
      return Result.success(user);
    } on FirebaseAuthException catch (e, st) {
      AppLogger.error('Email sign-up failed',
          error: e, stackTrace: st, tag: 'FirebaseAuthService');
      return Result.failure(_mapSignUpError(e, st));
    } catch (e, st) {
      return Result.failure(AppException.authFailed(e, st));
    }
  }

  AppException _mapSignInError(FirebaseAuthException e, StackTrace st) {
    switch (e.code) {
      case 'user-not-found':
      case 'wrong-password':
      case 'invalid-credential':
      case 'INVALID_LOGIN_CREDENTIALS':
        return AppException(
          code: ErrorCode.authFailed,
          message: 'Wrong email or password. Please try again.',
          originalError: e,
          stackTrace: st,
        );
      case 'invalid-email':
        return AppException(
          code: ErrorCode.authFailed,
          message: 'Enter a valid email address.',
          originalError: e,
          stackTrace: st,
        );
      case 'user-disabled':
        return AppException(
          code: ErrorCode.authFailed,
          message: 'This account has been disabled.',
          originalError: e,
          stackTrace: st,
        );
      case 'too-many-requests':
        return AppException(
          code: ErrorCode.authFailed,
          message: 'Too many attempts. Try again later.',
          originalError: e,
          stackTrace: st,
        );
      default:
        return AppException(
          code: ErrorCode.authFailed,
          message: "Sign-in didn't work. Please try again.",
          originalError: e,
          stackTrace: st,
        );
    }
  }

  AppException _mapSignUpError(FirebaseAuthException e, StackTrace st) {
    switch (e.code) {
      case 'email-already-in-use':
        return AppException(
          code: ErrorCode.authFailed,
          message: 'An account already exists with this email. Sign in instead.',
          originalError: e,
          stackTrace: st,
        );
      case 'weak-password':
        return AppException(
          code: ErrorCode.authFailed,
          message: 'Password must be at least 6 characters.',
          originalError: e,
          stackTrace: st,
        );
      case 'invalid-email':
        return AppException(
          code: ErrorCode.authFailed,
          message: 'Enter a valid email address.',
          originalError: e,
          stackTrace: st,
        );
      default:
        return AppException(
          code: ErrorCode.authFailed,
          message: "Couldn't create your account. Please try again.",
          originalError: e,
          stackTrace: st,
        );
    }
  }

  Future<Result<void>> signOut() async {
    final auth = _auth;
    try {
      final signOuts = <Future<void>>[_googleSignIn.signOut()];
      if (auth != null) {
        signOuts.add(auth.signOut());
      }
      await Future.wait(signOuts).timeout(const Duration(seconds: 8));
      AppLogger.info('Sign-out successful', tag: 'FirebaseAuthService');
      return const Result.success(null);
    } catch (e, st) {
      AppLogger.error(
        'Sign-out failed',
        error: e,
        stackTrace: st,
        tag: 'FirebaseAuthService',
      );
      return Result.failure(
        AppException.unexpected(e.toString(), error: e, stackTrace: st),
      );
    }
  }

  bool get isSignedIn => _auth?.currentUser != null;
}
