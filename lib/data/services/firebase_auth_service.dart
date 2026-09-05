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

  FirebaseAuthService({FirebaseAuth? auth, GoogleSignIn? googleSignIn})
    : _auth = auth ?? _resolveAuth(),
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
        })
        .catchError((Object error, StackTrace stackTrace) {
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
      final isUserCancellation =
          e.code == GoogleSignInExceptionCode.canceled &&
          (e.description == null ||
              e.description!.isEmpty ||
              e.description!.toLowerCase().contains('cancel'));
      if (isUserCancellation) {
        return Result.failure(AppException.authCancelled());
      }
      return Result.failure(AppException.authFailed(e, st));
    } on FirebaseAuthException catch (e, st) {
      return Result.failure(
        _mapFederatedSignInError(e, st, provider: 'Google'),
      );
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

  /// Uses Firebase's native Apple provider flow. The provider asks only for
  /// name and email, and Apple may return the name only on first approval.
  Future<Result<User>> signInWithApple() async {
    final auth = _auth;
    if (auth == null) {
      return Result.failure(
        AppException.unexpected(
          'Firebase authentication is not configured for this build.',
        ),
      );
    }

    try {
      final provider = AppleAuthProvider()
        ..addScope('email')
        ..addScope('name');
      final userCredential = await auth.signInWithProvider(provider);
      final user = userCredential.user;
      if (user == null) {
        return Result.failure(
          AppException.authFailed(Exception('No user after Apple sign-in')),
        );
      }

      AppLogger.info(
        'Apple sign-in successful',
        tag: 'FirebaseAuthService',
        metadata: {'uid': user.uid},
      );
      return Result.success(user);
    } on FirebaseAuthException catch (e, st) {
      return Result.failure(_mapFederatedSignInError(e, st, provider: 'Apple'));
    } catch (e, st) {
      AppLogger.error(
        'Apple sign-in failed',
        error: e,
        stackTrace: st,
        tag: 'FirebaseAuthService',
      );
      return Result.failure(AppException.authFailed(e, st));
    }
  }

  /// Maps the [FirebaseAuthException]s a federated credential exchange can
  /// raise. Apple and Google hit the identical set, so this lives in one place:
  /// the two providers previously carried separate copies of this mapping and
  /// neither handled the account-collision case below.
  ///
  /// Codes arrive already normalised by firebase_auth_platform_interface, which
  /// applies `replaceAll('ERROR_', '').toLowerCase().replaceAll('_', '-')`.
  /// That folds Android's raw `ERROR_SCREAMING_SNAKE` codes and iOS's
  /// already-kebab-case codes into the same spelling, so matching the kebab form
  /// covers both platforms.
  AppException _mapFederatedSignInError(
    FirebaseAuthException e,
    StackTrace st, {
    required String provider,
  }) {
    final code = e.code.toLowerCase();

    const cancellationCodes = {
      'canceled',
      'cancelled',
      'popup-closed-by-user',
      'web-context-canceled',
      'web-context-cancelled',
    };
    if (cancellationCodes.contains(code)) {
      // Backing out of the provider sheet is a normal action, not a crash, so
      // it is logged as info and never reaches Crashlytics.
      AppLogger.info(
        '$provider sign-in cancelled by user',
        tag: 'FirebaseAuthService',
      );
      return AppException.authCancelled();
    }

    AppLogger.error(
      '$provider sign-in credential exchange failed',
      error: e,
      stackTrace: st,
      tag: 'FirebaseAuthService',
    );

    // Firebase defaults to one account per email address, so a user who first
    // signed up with one provider and later taps another with the same email
    // lands here. Retrying the same button can never succeed, which is exactly
    // what the generic "Sign-in didn't work. Please try again." told them to do.
    // The plural spelling is what the Windows plugin emits
    // (firebase_auth/windows/firebase_auth_plugin.cpp), kept so a desktop caller
    // gets the same copy.
    if (code == 'account-exists-with-different-credential' ||
        code == 'account-exists-with-different-credentials') {
      // Firebase does not report WHICH provider owns the account, and
      // fetchSignInMethodsForEmail is inert under email-enumeration protection,
      // so the copy must not name one. [FirebaseAuthException.email] is
      // populated on both iOS and Android for this code, but is still optional.
      final email = e.email;
      final subject = email == null || email.isEmpty
          ? 'You already have an Aura account with this email.'
          : 'You already have an Aura account for $email.';
      return AppException(
        code: ErrorCode.authFailed,
        message:
            '$subject Sign in the way you did the first time — Google, Apple, '
            'or email — and everything will be right where you left it.',
        originalError: e,
        stackTrace: st,
      );
    }

    if (code == 'network-request-failed') {
      // Official Firebase code for connectivity loss during the credential
      // exchange — surface that honestly rather than blaming the provider.
      return AppException(
        code: ErrorCode.authFailed,
        message:
            "Looks like you're offline. Check your connection and try again.",
        originalError: e,
        stackTrace: st,
      );
    }

    // The provider is not enabled on the Firebase project, so the backend
    // rejected an otherwise valid credential ("The identity provider
    // configuration is not found"). The native sheet has already succeeded by
    // this point, so the user did nothing wrong and retrying cannot help —
    // point them at a provider that does work. The email handlers below have
    // always mapped this code; the federated path had not, which is how a
    // console misconfiguration reached a user as "Please try again."
    if (code == 'operation-not-allowed') {
      return AppException(
        code: ErrorCode.authFailed,
        message:
            '$provider sign-in is unavailable right now. Try another way to '
            'sign in.',
        originalError: e,
        stackTrace: st,
      );
    }

    return AppException.authFailed(e, st);
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
      final isUserCancellation =
          e.code == GoogleSignInExceptionCode.canceled &&
          (e.description == null ||
              e.description!.isEmpty ||
              e.description!.toLowerCase().contains('cancel'));
      if (isUserCancellation) {
        // User backed out of the account picker — a normal action, not a crash.
        // Log as a warning so it never reaches Crashlytics.
        AppLogger.warning(
          'Google server auth code request cancelled by user',
          tag: 'FirebaseAuthService',
          metadata: {'code': e.code.toString()},
        );
        return Result.failure(AppException.authCancelled());
      }
      AppLogger.error(
        'Google server auth code request failed',
        error: e,
        stackTrace: st,
        tag: 'FirebaseAuthService',
      );
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
    String email,
    String password,
  ) async {
    final auth = _auth;
    if (auth == null) {
      return Result.failure(
        AppException.unexpected('Firebase not configured.'),
      );
    }
    try {
      final credential = await auth.signInWithEmailAndPassword(
        email: email,
        password: password,
      );
      final user = credential.user;
      if (user == null) {
        return Result.failure(
          AppException.authFailed(Exception('No user returned.')),
        );
      }
      AppLogger.info(
        'Email sign-in successful',
        tag: 'FirebaseAuthService',
        metadata: {'uid': user.uid},
      );
      return Result.success(user);
    } on FirebaseAuthException catch (e, st) {
      AppLogger.error(
        'Email sign-in failed',
        error: e,
        stackTrace: st,
        tag: 'FirebaseAuthService',
      );
      return Result.failure(_mapSignInError(e, st));
    } catch (e, st) {
      return Result.failure(AppException.authFailed(e, st));
    }
  }

  Future<Result<User>> createUserWithEmailAndPassword(
    String email,
    String password,
    String name,
  ) async {
    final auth = _auth;
    if (auth == null) {
      return Result.failure(
        AppException.unexpected('Firebase not configured.'),
      );
    }
    try {
      final credential = await auth.createUserWithEmailAndPassword(
        email: email,
        password: password,
      );
      final user = credential.user;
      if (user == null) {
        return Result.failure(
          AppException.authFailed(Exception('No user returned.')),
        );
      }
      if (name.isNotEmpty) {
        await user.updateDisplayName(name);
      }
      AppLogger.info(
        'Email account created',
        tag: 'FirebaseAuthService',
        metadata: {'uid': user.uid},
      );
      return Result.success(user);
    } on FirebaseAuthException catch (e, st) {
      AppLogger.error(
        'Email sign-up failed',
        error: e,
        stackTrace: st,
        tag: 'FirebaseAuthService',
      );
      return Result.failure(_mapSignUpError(e, st));
    } catch (e, st) {
      return Result.failure(AppException.authFailed(e, st));
    }
  }

  /// Asks Firebase to email a password reset link.
  ///
  /// The email address is never verified by us, and deliberately so: the link
  /// itself IS the proof of ownership, because only someone who controls that
  /// inbox can open it. There is nothing to pre-check.
  ///
  /// Consequently this MUST NOT tell the caller whether an account exists.
  /// `user-not-found` is mapped to success so the UI shows one message either
  /// way — otherwise the reset form becomes an oracle for "is this person on
  /// Aura?", which is exactly the account-enumeration leak the Identity
  /// Platform's email enumeration protection exists to close. That protection
  /// already suppresses the error server-side ("there are no specific error
  /// messages indicating when emails aren't sent"), so this mapping is defence
  /// in depth for the case where it is ever switched off on the project.
  ///
  /// Format and transport problems ARE surfaced, since a malformed address or a
  /// dead connection discloses nothing about who has an account.
  Future<Result<void>> sendPasswordResetEmail(String email) async {
    final auth = _auth;
    if (auth == null) {
      return Result.failure(
        AppException.unexpected('Firebase not configured.'),
      );
    }
    try {
      await auth.sendPasswordResetEmail(email: email);
      AppLogger.info(
        'Password reset email requested',
        tag: 'FirebaseAuthService',
      );
      return const Result.success(null);
    } on FirebaseAuthException catch (e, st) {
      if (e.code == 'user-not-found') {
        // Deliberately indistinguishable from success. Logged at info with no
        // address so the event is traceable without recording who was probed.
        AppLogger.info(
          'Password reset requested for an address with no account',
          tag: 'FirebaseAuthService',
        );
        return const Result.success(null);
      }
      AppLogger.error(
        'Password reset request failed',
        error: e,
        stackTrace: st,
        tag: 'FirebaseAuthService',
      );
      return Result.failure(_mapPasswordResetError(e, st));
    } catch (e, st) {
      AppLogger.error(
        'Password reset request failed',
        error: e,
        stackTrace: st,
        tag: 'FirebaseAuthService',
      );
      return Result.failure(AppException.authFailed(e, st));
    }
  }

  AppException _mapPasswordResetError(FirebaseAuthException e, StackTrace st) {
    switch (e.code) {
      case 'invalid-email':
        return AppException(
          code: ErrorCode.authFailed,
          message: 'Enter a valid email address.',
          originalError: e,
          stackTrace: st,
        );
      case 'too-many-requests':
        return AppException(
          code: ErrorCode.authFailed,
          message: 'Too many attempts. Try again in a few minutes.',
          originalError: e,
          stackTrace: st,
        );
      case 'network-request-failed':
        return AppException(
          code: ErrorCode.authFailed,
          message:
              "Looks like you're offline. Check your connection and try again.",
          originalError: e,
          stackTrace: st,
        );
      default:
        return AppException(
          code: ErrorCode.authFailed,
          message: "Couldn't send the reset link. Please try again.",
          originalError: e,
          stackTrace: st,
        );
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
          message: 'Too many attempts. Try again in a few minutes.',
          originalError: e,
          stackTrace: st,
        );
      case 'network-request-failed':
        // Official Firebase code for connectivity loss. Mapped explicitly so the
        // user isn't told their password is wrong when they're really offline.
        return AppException(
          code: ErrorCode.authFailed,
          message:
              "Looks like you're offline. Check your connection and try again.",
          originalError: e,
          stackTrace: st,
        );
      case 'operation-not-allowed':
        return AppException(
          code: ErrorCode.authFailed,
          message:
              'Email sign-in is unavailable right now. Try continuing with Google.',
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
          message:
              'An account already exists with this email. Sign in instead.',
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
      case 'too-many-requests':
        // Firebase rate-limits account creation too, not just sign-in.
        return AppException(
          code: ErrorCode.authFailed,
          message: 'Too many attempts. Try again in a few minutes.',
          originalError: e,
          stackTrace: st,
        );
      case 'network-request-failed':
        return AppException(
          code: ErrorCode.authFailed,
          message:
              "Looks like you're offline. Check your connection and try again.",
          originalError: e,
          stackTrace: st,
        );
      case 'operation-not-allowed':
        return AppException(
          code: ErrorCode.authFailed,
          message:
              'Email sign-up is unavailable right now. Try continuing with Google.',
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
      // Firebase sign-out is the one that actually ends the session, so it must
      // run unconditionally and its result decides success. The Google session
      // clear is best-effort and MUST NOT block it: google_sign_in has no
      // Windows implementation (desktop signs in via a pairing custom token, not
      // Google), so on desktop this throws and is deliberately swallowed. Keeping
      // it in its own try means desktop still signs out and mobile Google users
      // still get their Google session cleared.
      if (auth != null) {
        await auth.signOut().timeout(const Duration(seconds: 8));
      }
      try {
        await _googleSignIn.signOut().timeout(const Duration(seconds: 5));
      } catch (e) {
        AppLogger.info(
          'Google session clear skipped (unsupported or not signed in with Google)',
          tag: 'FirebaseAuthService',
        );
      }
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
