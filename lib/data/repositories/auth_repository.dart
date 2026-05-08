import 'package:firebase_auth/firebase_auth.dart';
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
  Stream<UserModel?> get userModelStream =>
      _authService.authStateStream.asyncMap((firebaseUser) async {
        if (firebaseUser == null) return null;
        final result = await getCurrentUser();
        return result.when(
          success: (user) => user,
          failure: (_) => null,
        );
      });

  User? get currentFirebaseUser => _authService.currentUser;

  Future<Result<UserModel>> signInWithGoogle() async {
    final authResult = await _authService.signInWithGoogle();
    return authResult.when(
      success: (user) async {
        return _getOrCreateUser(user);
      },
      failure: (error) => Future.value(Result.failure(error)),
    );
  }

  Future<Result<UserModel>> _getOrCreateUser(User firebaseUser) async {
    final existingResult = await _firestoreService.getDocument(
      AppConstants.usersCollection,
      firebaseUser.uid,
      UserModel.fromJson,
    );

    return existingResult.when(
      success: (user) async {
        // Detect timezone on every sign-in so it stays accurate if the user travels
        final timezone = await _detectTimezone();
        final updated = user.copyWith(
          lastActiveAt: DateTime.now(),
          timezone: timezone,
        );
        await _firestoreService.updateDocument(
          AppConstants.usersCollection,
          firebaseUser.uid,
          {
            'last_active_at': DateTime.now().toUtc().toIso8601String(),
            'timezone': timezone,
          },
        );
        return Result.success(updated);
      },
      failure: (error) async {
        if (error.code == ErrorCode.documentNotFound) {
          return _createUser(firebaseUser);
        }
        return Result.failure(error);
      },
    );
  }

  Future<Result<UserModel>> _createUser(User firebaseUser) async {
    final now = DateTime.now();
    final timezone = await _detectTimezone();
    final user = UserModel(
      uid: firebaseUser.uid,
      displayName: firebaseUser.displayName ?? 'User',
      email: firebaseUser.email ?? '',
      photoUrl: firebaseUser.photoURL,
      settings: UserSettings.defaults(),
      createdAt: now,
      lastActiveAt: now,
      timezone: timezone,
    );

    AppLogger.info(
      'Creating new user document',
      tag: 'AuthRepository',
      metadata: {'uid': firebaseUser.uid},
    );

    final json = user.toJson();
    json.remove('id'); // Firestore uses doc ID separately

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
      success: (user) async => _getOrCreateUser(user),
      failure: (error) => Future.value(Result.failure(error)),
    );
  }

  Future<Result<void>> signOut() async {
    return _authService.signOut();
  }

  Future<String?> getIdToken() => _authService.getIdToken();
}
