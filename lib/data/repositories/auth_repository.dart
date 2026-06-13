import 'package:cloud_firestore/cloud_firestore.dart';
import 'package:firebase_auth/firebase_auth.dart';
import 'package:flutter/foundation.dart';
import 'package:flutter_timezone/flutter_timezone.dart';
import '../../core/constants/app_constants.dart';
import '../../core/errors/app_exception.dart';
import '../../core/logging/app_logger.dart';
import '../../core/network/api_response.dart';
import '../models/user_model.dart';
import '../services/firebase_auth_service.dart';
import '../services/firestore_service.dart';

class AuthRepository {
  final FirebaseAuthService _authService;
  final FirestoreService _firestoreService;

  AuthRepository({
    required FirebaseAuthService authService,
    required FirestoreService firestoreService,
  })  : _authService = authService,
        _firestoreService = firestoreService;

  Stream<User?> get authStateStream => _authService.authStateStream;

  // Maps the raw Firebase auth stream to UserModel? so AuthViewModel
  // doesn't need to import firebase_auth directly.
  // Uses _getOrCreateUser so the stream never emits null while a Firestore
  // doc is still being created (race condition on first sign-in).
  Stream<UserModel?> get userModelStream =>
      _authService.authStateStream.asyncMap((firebaseUser) async {
        if (firebaseUser == null) return null;
        final result = await _getOrCreateUser(firebaseUser, name: null);
        return result.when(
          success: (user) => user,
          failure: (_) => null,
        );
      });

  User? get currentFirebaseUser => _authService.currentUser;

  Future<Result<UserModel>> signInWithGoogle() async {
    final authResult = await _authService.signInWithGoogle();
    return authResult.when(
      success: (user) =>
          _completeSignIn(user, name: null, signInMethod: 'google'),
      failure: (error) => Future.value(Result.failure(error)),
    );
  }

  /// Runs only after a real, user-initiated sign-in (not a silent session
  /// restore through [userModelStream]): ensures the user doc exists and folds
  /// the login metadata into that same doc write, so one sign-in is one Firestore write. 
  /// The silent-restore path calls [_getOrCreateUser] with no metadata and only refreshes activity.
  Future<Result<UserModel>> _completeSignIn(
    User firebaseUser, {
    required String? name,
    required String signInMethod,
  }) {
    return _getOrCreateUser(
      firebaseUser,
      name: name,
      loginMetadata: _loginMetadataFields(signInMethod),
    );
  }

  /// Builds the login metadata stamped onto the user doc on a real sign-in:
  /// last_login_at, an atomic login_count bump, the active flag, and how/where
  /// they signed in. Merged into the single get-or-create doc write rather than
  /// written on its own, so a sign-in touches the doc once. [FieldValue.increment]
  /// keeps the counter atomic and starts from 0 on docs written by older clients.
  Map<String, dynamic> _loginMetadataFields(String signInMethod) => {
        UserModel.fieldLastLoginAt: DateTime.now().toUtc().toIso8601String(),
        UserModel.fieldLoginCount: FieldValue.increment(1),
        UserModel.fieldIsActive: true,
        UserModel.fieldSignInMethod: signInMethod,
        UserModel.fieldPlatform: _platformName(),
      };

  /// Stamps last_logout_at, increments logout_count, and clears the active flag.
  /// Must run while Firebase auth is still valid — Firestore rules reject the
  /// write once the session is cleared.
  Future<void> _recordLogoutMetadata(String uid) async {
    final result = await _firestoreService.updateDocument(
      AppConstants.usersCollection,
      uid,
      {
        UserModel.fieldLastLogoutAt: DateTime.now().toUtc().toIso8601String(),
        UserModel.fieldLogoutCount: FieldValue.increment(1),
        UserModel.fieldIsActive: false,
      },
    );
    result.when(
      success: (_) {},
      failure: (error) => AppLogger.warning(
        'Failed to record logout metadata (non-blocking)',
        tag: 'AuthRepository',
        metadata: {'uid': uid, 'error': error.message},
      ),
    );
  }

  String _platformName() {
    switch (defaultTargetPlatform) {
      case TargetPlatform.iOS:
        return 'ios';
      case TargetPlatform.android:
        return 'android';
      default:
        return defaultTargetPlatform.name;
    }
  }

  /// Loads the user doc (creating it if missing) and refreshes activity on every
  /// call. [loginMetadata] is non-null only on a real sign-in (not silent session
  /// restore); when present it is merged into the same doc write so the sign-in
  /// touches Firestore once instead of twice.
  Future<Result<UserModel>> _getOrCreateUser(
      User firebaseUser, {
      required String? name,
      Map<String, dynamic>? loginMetadata}) async {
    final existingResult = await _firestoreService.getDocument(
      AppConstants.usersCollection,
      firebaseUser.uid,
      UserModel.fromJson,
    );

    return existingResult.when(
      success: (user) async {
        // Detect timezone on every sign-in so it stays accurate if the user travels
        final timezone = await _detectTimezone();
        final now = DateTime.now();
        final updated = user.copyWith(
          lastActiveAt: now,
          timezone: timezone,
        );
        final writeResult = await _firestoreService.updateDocument(
          AppConstants.usersCollection,
          firebaseUser.uid,
          {
            'last_active_at': now.toUtc().toIso8601String(),
            'timezone': timezone,
            ...?loginMetadata,
          },
        );
        writeResult.when(
          success: (_) {},
          failure: (error) => AppLogger.warning(
            'Failed to refresh activity/login metadata (non-blocking)',
            tag: 'AuthRepository',
            metadata: {'uid': firebaseUser.uid, 'error': error.message},
          ),
        );
        return Result.success(updated);
      },
      failure: (error) async {
        if (error.code == ErrorCode.documentNotFound) {
          return _createUser(firebaseUser, name: name, loginMetadata: loginMetadata);
        }
        return Result.failure(error);
      },
    );
  }

  Future<Result<UserModel>> _createUser(
      User firebaseUser, {
      required String? name,
      Map<String, dynamic>? loginMetadata}) async {
    final now = DateTime.now();
    final timezone = await _detectTimezone();
    final resolvedName =
        (name != null && name.isNotEmpty) ? name : (firebaseUser.displayName ?? 'User');
    final user = UserModel(
      uid: firebaseUser.uid,
      displayName: resolvedName,
      email: firebaseUser.email ?? '',
      photoUrl: firebaseUser.photoURL,
      settings: UserSettings.defaults(),
      createdAt: now,
      lastActiveAt: now,
      timezone: timezone,
      onboardingComplete: false,
    );

    AppLogger.info(
      'Creating new user document',
      tag: 'AuthRepository',
      metadata: {'uid': firebaseUser.uid},
    );

    final json = user.toJson();
    json.remove('id'); // Firestore uses doc ID separately
    // Fold login metadata (when this is a real sign-in) into the create write so
    // a new user is stamped in one write instead of a create + follow-up update.
    if (loginMetadata != null) json.addAll(loginMetadata);

    final result = await _firestoreService.setDocument(
      AppConstants.usersCollection,
      firebaseUser.uid,
      json,
      UserModel.fromJson,
    );

    return result;
  }

  /// Detects the device's IANA timezone string (e.g. "Asia/Kolkata").
  /// Returns "UTC" if detection fails — the backend handles this gracefully.
  Future<String> _detectTimezone() async {
    try {
      final tz = await FlutterTimezone.getLocalTimezone();
      return tz.identifier;
    } catch (e) {
      AppLogger.warning(
        'Timezone detection failed, defaulting to UTC',
        tag: 'AuthRepository',
      );
      return 'UTC';
    }
  }

  Future<Result<UserModel?>> getCurrentUser() async {
    final firebaseUser = _authService.currentUser;
    if (firebaseUser == null) return const Result.success(null);

    final result = await _firestoreService.getDocument(
      AppConstants.usersCollection,
      firebaseUser.uid,
      UserModel.fromJson,
    );

    return result.when(
      success: (user) => Result.success(user),
      failure: (error) {
        if (error.code == ErrorCode.documentNotFound) {
          return const Result.success(null);
        }
        return Result.failure(error);
      },
    );
  }

  Future<Result<UserModel>> signInWithEmail(
      String email, String password) async {
    final authResult =
        await _authService.signInWithEmailAndPassword(email, password);
    return authResult.when(
      success: (user) =>
          _completeSignIn(user, name: null, signInMethod: 'password'),
      failure: (error) => Future.value(Result.failure(error)),
    );
  }

  Future<Result<UserModel>> createAccountWithEmail(
      String email, String password, String name) async {
    final authResult =
        await _authService.createUserWithEmailAndPassword(email, password, name);
    return authResult.when(
      success: (user) =>
          _completeSignIn(user, name: name, signInMethod: 'password'),
      failure: (error) => Future.value(Result.failure(error)),
    );
  }

  Future<Result<void>> signOut() async {
    // Record logout while the session is still authenticated; once
    // _authService.signOut() runs, Firestore rules reject writes to the doc.
    final uid = _authService.currentUser?.uid;
    if (uid != null) {
      await _recordLogoutMetadata(uid);
    }
    return _authService.signOut();
  }

  Future<String?> getIdToken() => _authService.getIdToken();
}
