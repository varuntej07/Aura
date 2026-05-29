import 'dart:async';

import 'package:flutter_test/flutter_test.dart';
import 'package:mockito/annotations.dart';
import 'package:mockito/mockito.dart';

import 'package:aura/core/errors/app_exception.dart';
import 'package:aura/core/network/api_response.dart';
import 'package:aura/data/models/user_model.dart';
import 'package:aura/data/repositories/auth_repository.dart';
import 'package:aura/data/services/backend_api_service.dart';
import 'package:aura/data/services/notification_service.dart';
import 'package:aura/data/services/posthog_analytics_service.dart';
import 'package:aura/presentation/viewmodels/auth_viewmodel.dart';

import 'auth_viewmodel_test.mocks.dart';

@GenerateNiceMocks([
  MockSpec<AuthRepository>(),
  MockSpec<NotificationService>(),
  MockSpec<BackendApiService>(),
  MockSpec<PostHogAnalyticsService>(),
])
UserModel _user({
  String uid = 'uid-1',
  bool onboardingComplete = true,
  bool? auraConsentGranted,
}) {
  return UserModel(
    uid: uid,
    displayName: 'Test User',
    email: 'test@example.com',
    settings: UserSettings.defaults(),
    createdAt: DateTime(2026, 1, 1),
    lastActiveAt: DateTime(2026, 1, 1),
    onboardingComplete: onboardingComplete,
    auraConsentGranted: auraConsentGranted,
  );
}

void main() {
  setUpAll(() {
    provideDummy<Result<UserModel>>(
      Result.failure(AppException.unexpected('dummy')),
    );
    provideDummy<Result<void>>(const Result.success(null));
  });

  late MockAuthRepository authRepository;
  late MockNotificationService notificationService;
  late MockBackendApiService backendApiService;
  late MockPostHogAnalyticsService postHog;
  late AuthViewModel vm;
  late StreamController<UserModel?> authStream;

  setUp(() {
    authRepository = MockAuthRepository();
    notificationService = MockNotificationService();
    backendApiService = MockBackendApiService();
    postHog = MockPostHogAnalyticsService();
    authStream = StreamController<UserModel?>.broadcast();

    when(authRepository.userModelStream).thenAnswer((_) => authStream.stream);
    when(authRepository.signOut())
        .thenAnswer((_) async => const Result.success(null));
    when(notificationService.initialize(any)).thenAnswer((_) async {});
    when(postHog.identifyUser(any)).thenAnswer((_) async {});
    when(postHog.reset()).thenAnswer((_) async {});

    vm = AuthViewModel(
      authRepository: authRepository,
      notificationService: notificationService,
      backendApiService: backendApiService,
      postHogAnalyticsService: postHog,
    );
  });

  tearDown(() async {
    await authStream.close();
    vm.dispose();
  });

  group('initialize — auth stream', () {
    test('onboarded user → loaded, authenticated, no onboarding needed',
        () async {
      await vm.initialize();
      authStream.add(_user(onboardingComplete: true));
      await pumpEventQueue();

      expect(vm.state, ViewState.loaded);
      expect(vm.isAuthenticated, isTrue);
      expect(vm.needsOnboarding, isFalse);
      verify(notificationService.initialize('uid-1')).called(1);
      verify(postHog.identifyUser('uid-1')).called(1);
    });

    test('user with onboarding incomplete → needsOnboarding', () async {
      await vm.initialize();
      authStream.add(_user(onboardingComplete: false));
      await pumpEventQueue();

      expect(vm.isAuthenticated, isTrue);
      expect(vm.needsOnboarding, isTrue);
    });

    test('null emission → idle, not authenticated', () async {
      await vm.initialize();
      authStream.add(null);
      await pumpEventQueue();

      expect(vm.state, ViewState.idle);
      expect(vm.isAuthenticated, isFalse);
    });

    test('stream error → error state', () async {
      await vm.initialize();
      authStream.addError(Exception('boom'));
      await pumpEventQueue();

      expect(vm.state, ViewState.error);
      expect(vm.error, isNotNull);
    });
  });

  group('signInWithGoogle', () {
    test('success → loaded + user set', () async {
      when(authRepository.signInWithGoogle())
          .thenAnswer((_) async => Result.success(_user()));

      await vm.signInWithGoogle();

      expect(vm.state, ViewState.loaded);
      expect(vm.user?.uid, 'uid-1');
      expect(vm.error, isNull);
    });

    test('failure → error state', () async {
      when(authRepository.signInWithGoogle())
          .thenAnswer((_) async => Result.failure(AppException.authFailed(Exception('x'))));

      await vm.signInWithGoogle();

      expect(vm.state, ViewState.error);
      expect(vm.error, isNotNull);
      expect(vm.user, isNull);
    });
  });

  group('signInWithEmail', () {
    test('success → loaded + user set', () async {
      when(authRepository.signInWithEmail(any, any))
          .thenAnswer((_) async => Result.success(_user()));

      await vm.signInWithEmail('test@example.com', 'pw');

      expect(vm.state, ViewState.loaded);
      expect(vm.user?.uid, 'uid-1');
    });

    test('failure → error state', () async {
      when(authRepository.signInWithEmail(any, any))
          .thenAnswer((_) async => Result.failure(AppException.authFailed(Exception('x'))));

      await vm.signInWithEmail('test@example.com', 'pw');

      expect(vm.state, ViewState.error);
      expect(vm.error, isNotNull);
    });
  });

  group('markOnboardingComplete', () {
    test('flips onboarding flags and sets justCompletedOnboarding', () async {
      when(authRepository.signInWithGoogle())
          .thenAnswer((_) async => Result.success(_user(onboardingComplete: false)));
      await vm.signInWithGoogle();

      vm.markOnboardingComplete(auraConsentGranted: true);

      expect(vm.needsOnboarding, isFalse);
      expect(vm.user?.auraConsentGranted, isTrue);
      expect(vm.justCompletedOnboarding, isTrue);

      vm.consumeFirstSessionPrompt();
      expect(vm.justCompletedOnboarding, isFalse);
    });

    test('no-op when no user is set', () {
      vm.markOnboardingComplete(auraConsentGranted: true);
      expect(vm.user, isNull);
      expect(vm.justCompletedOnboarding, isFalse);
    });
  });

  group('signOut', () {
    test('clears user, goes idle, delegates to repo + posthog reset', () async {
      when(authRepository.signInWithGoogle())
          .thenAnswer((_) async => Result.success(_user()));
      await vm.signInWithGoogle();

      await vm.signOut();

      expect(vm.user, isNull);
      expect(vm.state, ViewState.idle);
      verify(authRepository.signOut()).called(1);
      verify(postHog.reset()).called(1);
    });
  });

  group('deleteAccount', () {
    test('success → returns null, clears user, signs out', () async {
      when(backendApiService.deleteAccount())
          .thenAnswer((_) async => const Result.success(null));

      final result = await vm.deleteAccount();

      expect(result, isNull);
      expect(vm.user, isNull);
      verify(authRepository.signOut()).called(1);
    });

    test('failure → returns error string, state loaded', () async {
      when(backendApiService.deleteAccount())
          .thenAnswer((_) async => Result.failure(AppException.unexpected('nope')));

      final result = await vm.deleteAccount();

      expect(result, isNotNull);
      expect(vm.state, ViewState.loaded);
    });
  });

  test('clearError clears the error', () async {
    when(authRepository.signInWithGoogle())
        .thenAnswer((_) async => Result.failure(AppException.authFailed(Exception('x'))));
    await vm.signInWithGoogle();
    expect(vm.error, isNotNull);

    vm.clearError();
    expect(vm.error, isNull);
  });
}
