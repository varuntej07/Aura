import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';
import 'package:mockito/mockito.dart';
import 'package:provider/provider.dart';

import 'package:aura/core/errors/app_exception.dart';
import 'package:aura/core/network/api_response.dart';
import 'package:aura/data/models/user_model.dart';
import 'package:aura/presentation/viewmodels/auth_viewmodel.dart';
import 'package:aura/presentation/widgets/sign_in_gate_dialog.dart';

// Reuse the NiceMocks generated for the AuthViewModel suite so we don't
// regenerate the same mock classes here.
import '../viewmodels/auth_viewmodel_test.mocks.dart';

UserModel _user({bool onboardingComplete = true}) {
  return UserModel(
    uid: 'uid-1',
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

  setUp(() {
    authRepository = MockAuthRepository();
    notificationService = MockNotificationService();
    backendApiService = MockBackendApiService();
    postHog = MockPostHogAnalyticsService();

    when(notificationService.initialize(any)).thenAnswer((_) async {});
    when(postHog.identifyUser(any)).thenAnswer((_) async {});

    vm = AuthViewModel(
      authRepository: authRepository,
      notificationService: notificationService,
      backendApiService: backendApiService,
      postHogAnalyticsService: postHog,
    );
  });

  tearDown(() => vm.dispose());

  // A minimal app that mirrors the real bug surface: the gate dialog is shown
  // from a route that stays VALID for a logged-in user (like /chat or
  // /briefing), so the router redirect alone never moves the user. Only the
  // dialog's own post-sign-in navigation should land them on /home.
  Widget buildApp() {
    final router = GoRouter(
      initialLocation: '/gated',
      routes: [
        GoRoute(
          path: '/gated',
          builder: (context, state) => Scaffold(
            body: Center(
              child: ElevatedButton(
                onPressed: () => showSignInGateDialog(context),
                child: const Text('open gate'),
              ),
            ),
          ),
        ),
        GoRoute(
          path: '/home',
          builder: (context, state) =>
              const Scaffold(body: Center(child: Text('HOME SCREEN'))),
        ),
        GoRoute(
          path: '/login',
          builder: (context, state) =>
              const Scaffold(body: Center(child: Text('LOGIN SCREEN'))),
        ),
      ],
    );

    return ChangeNotifierProvider<AuthViewModel>.value(
      value: vm,
      child: MaterialApp.router(routerConfig: router),
    );
  }

  testWidgets(
      'Continue with Google: on success, leaves the gated screen and lands on /home',
      (tester) async {
    when(authRepository.signInWithGoogle())
        .thenAnswer((_) async => Result.success(_user()));

    await tester.pumpWidget(buildApp());
    await tester.pumpAndSettle();

    await tester.tap(find.text('open gate'));
    await tester.pumpAndSettle();
    expect(find.text('Continue with Google'), findsOneWidget);

    await tester.tap(find.text('Continue with Google'));
    await tester.pumpAndSettle();

    // The regression: before the fix the user stayed on the gated screen with
    // the dialog dismissed and nothing else changed.
    expect(find.text('HOME SCREEN'), findsOneWidget);
    expect(find.text('open gate'), findsNothing);
    verify(authRepository.signInWithGoogle()).called(1);
  });

  testWidgets(
      'Continue with Google: cancelled sign-in stays put (no navigation)',
      (tester) async {
    when(authRepository.signInWithGoogle()).thenAnswer(
      (_) async => Result.failure(AppException.authCancelled()),
    );

    await tester.pumpWidget(buildApp());
    await tester.pumpAndSettle();

    await tester.tap(find.text('open gate'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Continue with Google'));
    await tester.pumpAndSettle();

    // Backed out of the Google picker: not authenticated, so we do not navigate.
    expect(find.text('HOME SCREEN'), findsNothing);
    expect(find.text('open gate'), findsOneWidget);
  });
}
