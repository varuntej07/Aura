import 'package:firebase_auth/firebase_auth.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:mockito/annotations.dart';
import 'package:mockito/mockito.dart';

import 'package:aura/core/errors/app_exception.dart';
import 'package:aura/core/network/api_response.dart';
import 'package:aura/data/models/user_model.dart';
import 'package:aura/data/repositories/auth_repository.dart';
import 'package:aura/data/services/firebase_auth_service.dart';
import 'package:aura/data/services/firestore_service.dart';

import 'auth_repository_test.mocks.dart';

@GenerateNiceMocks([
  MockSpec<FirebaseAuthService>(),
  MockSpec<FirestoreService>(),
  MockSpec<User>(),
])
UserModel _user({String uid = 'uid-1', bool onboardingComplete = true}) {
  return UserModel(
    uid: uid,
    displayName: 'Test User',
    email: 'test@example.com',
    settings: UserSettings.defaults(),
    createdAt: DateTime(2026, 1, 1),
    lastActiveAt: DateTime(2026, 1, 1),
    onboardingComplete: onboardingComplete,
  );
}

void main() {
  setUpAll(() {
    provideDummy<Result<User>>(
      Result.failure(AppException.authFailed(Exception('dummy'))),
    );
    provideDummy<Result<UserModel>>(
      Result.failure(AppException.unexpected('dummy')),
    );
    provideDummy<Result<UserModel?>>(const Result.success(null));
    provideDummy<Result<void>>(const Result.success(null));
  });

  late MockFirebaseAuthService authService;
  late MockFirestoreService firestore;
  late MockUser firebaseUser;
  late AuthRepository repo;

  setUp(() {
    authService = MockFirebaseAuthService();
    firestore = MockFirestoreService();
    firebaseUser = MockUser();

    when(firebaseUser.uid).thenReturn('uid-1');
    when(firebaseUser.displayName).thenReturn('Test User');
    when(firebaseUser.email).thenReturn('test@example.com');
    when(firebaseUser.photoURL).thenReturn(null);

    when(firestore.updateDocument(any, any, any))
        .thenAnswer((_) async => const Result.success(null));

    repo = AuthRepository(authService: authService, firestoreService: firestore);
  });

  group('signInWithGoogle', () {
    test('existing user → success, refreshes last_active_at + timezone', () async {
      when(authService.signInWithGoogle())
          .thenAnswer((_) async => Result.success(firebaseUser));
      when(firestore.getDocument<UserModel>(any, any, any))
          .thenAnswer((_) async => Result.success(_user()));

      final result = await repo.signInWithGoogle();

      expect(result.isSuccess, isTrue);
      expect(result.dataOrNull?.uid, 'uid-1');
      verify(firestore.updateDocument(any, any, any)).called(1);
    });

    test('auth failure propagates', () async {
      when(authService.signInWithGoogle())
          .thenAnswer((_) async => Result.failure(AppException.authCancelled()));

      final result = await repo.signInWithGoogle();

      expect(result.isFailure, isTrue);
      verifyNever(firestore.getDocument<UserModel>(any, any, any));
    });
  });

  group('get-or-create', () {
    test('documentNotFound → creates user with onboarding incomplete', () async {
      when(authService.signInWithGoogle())
          .thenAnswer((_) async => Result.success(firebaseUser));
      when(firestore.getDocument<UserModel>(any, any, any)).thenAnswer(
        (_) async => Result.failure(
          const AppException(
            code: ErrorCode.documentNotFound,
            message: 'not found',
          ),
        ),
      );
      when(firestore.setDocument<UserModel>(any, any, any, any))
          .thenAnswer((_) async => Result.success(_user(onboardingComplete: false)));

      final result = await repo.signInWithGoogle();

      expect(result.isSuccess, isTrue);
      expect(result.dataOrNull?.onboardingComplete, isFalse);

      final data = verify(firestore.setDocument<UserModel>(
        any,
        any,
        captureAny,
        any,
      )).captured.single as Map<String, dynamic>;
      expect(data['onboarding_complete'], isFalse);
    });
  });

  group('signInWithEmail', () {
    test('existing user -> success, does not create Firestore doc', () async {
      when(authService.signInWithEmailAndPassword(any, any))
          .thenAnswer((_) async => Result.success(firebaseUser));
      when(firestore.getDocument<UserModel>(any, any, any))
          .thenAnswer((_) async => Result.success(_user()));

      final result = await repo.signInWithEmail('test@example.com', 'pw');

      expect(result.isSuccess, isTrue);
      expect(result.dataOrNull?.email, 'test@example.com');
      verifyNever(firestore.setDocument<UserModel>(any, any, any, any));
    });

    test('failure propagates', () async {
      when(authService.signInWithEmailAndPassword(any, any))
          .thenAnswer((_) async => Result.failure(AppException.authFailed(Exception('x'))));

      final result = await repo.signInWithEmail('test@example.com', 'pw');

      expect(result.isFailure, isTrue);
    });
  });

  group('createAccountWithEmail', () {
    test('new user → Firestore doc contains provided name', () async {
      when(authService.createUserWithEmailAndPassword(any, any, any))
          .thenAnswer((_) async => Result.success(firebaseUser));
      when(firestore.getDocument<UserModel>(any, any, any)).thenAnswer(
        (_) async => Result.failure(
          const AppException(code: ErrorCode.documentNotFound, message: 'not found'),
        ),
      );
      when(firestore.setDocument<UserModel>(any, any, any, any))
          .thenAnswer((_) async => Result.success(_user()));

      await repo.createAccountWithEmail('test@example.com', 'pw', 'Alice');

      final data = verify(firestore.setDocument<UserModel>(
        any, any, captureAny, any,
      )).captured.single as Map<String, dynamic>;
      expect(data['display_name'], 'Alice');
    });

    test('failure propagates', () async {
      when(authService.createUserWithEmailAndPassword(any, any, any))
          .thenAnswer((_) async => Result.failure(AppException.authFailed(Exception('x'))));

      final result =
          await repo.createAccountWithEmail('test@example.com', 'pw', 'Alice');

      expect(result.isFailure, isTrue);
    });
  });

  group('getCurrentUser', () {
    test('no firebase user → success(null)', () async {
      when(authService.currentUser).thenReturn(null);

      final result = await repo.getCurrentUser();

      expect(result.isSuccess, isTrue);
      expect(result.dataOrNull, isNull);
    });

    test('documentNotFound → success(null)', () async {
      when(authService.currentUser).thenReturn(firebaseUser);
      when(firestore.getDocument<UserModel>(any, any, any)).thenAnswer(
        (_) async => Result.failure(
          const AppException(
            code: ErrorCode.documentNotFound,
            message: 'not found',
          ),
        ),
      );

      final result = await repo.getCurrentUser();

      expect(result.isSuccess, isTrue);
      expect(result.dataOrNull, isNull);
    });
  });

  test('signOut delegates to auth service', () async {
    when(authService.signOut())
        .thenAnswer((_) async => const Result.success(null));

    await repo.signOut();

    verify(authService.signOut()).called(1);
  });
}
