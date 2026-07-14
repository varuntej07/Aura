import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:mockito/mockito.dart';
import 'package:provider/provider.dart';

import 'package:aura/core/network/api_response.dart';
import 'package:aura/data/models/user_model.dart';
import 'package:aura/presentation/screens/auth/login_screen.dart';
import 'package:aura/presentation/viewmodels/auth_viewmodel.dart';

import '../viewmodels/auth_viewmodel_test.mocks.dart';

UserModel _user() => UserModel(
  uid: 'uid-apple',
  displayName: 'Apple User',
  email: 'private@privaterelay.appleid.com',
  settings: UserSettings.defaults(),
  createdAt: DateTime(2026, 1, 1),
  lastActiveAt: DateTime(2026, 1, 1),
  onboardingComplete: true,
);

void main() {
  setUpAll(() {
    provideDummy<Result<UserModel>>(Result.success(_user()));
  });

  late MockAuthRepository authRepository;
  late MockNotificationService notificationService;
  late MockBackendApiService backendApiService;
  late MockPostHogAnalyticsService postHog;
  late AuthViewModel viewModel;

  setUp(() {
    authRepository = MockAuthRepository();
    notificationService = MockNotificationService();
    backendApiService = MockBackendApiService();
    postHog = MockPostHogAnalyticsService();
    when(
      notificationService.entitlementUpdatedStream,
    ).thenAnswer((_) => const Stream.empty());
    viewModel = AuthViewModel(
      authRepository: authRepository,
      notificationService: notificationService,
      backendApiService: backendApiService,
      postHogAnalyticsService: postHog,
    );
  });

  tearDown(() {
    viewModel.dispose();
  });

  Widget buildScreen(TargetPlatform platform) =>
      ChangeNotifierProvider<AuthViewModel>.value(
        value: viewModel,
        child: MaterialApp(
          theme: ThemeData(platform: platform),
          home: const LoginScreen(),
        ),
      );

  testWidgets('shows Apple sign-in on iOS and invokes the Apple auth flow', (
    tester,
  ) async {
    when(
      authRepository.signInWithApple(),
    ).thenAnswer((_) async => Result.success(_user()));

    await tester.pumpWidget(buildScreen(TargetPlatform.iOS));
    await tester.pumpAndSettle();

    expect(find.text('Continue with Apple'), findsOneWidget);
    expect(find.text('Continue with Google'), findsOneWidget);

    await tester.tap(find.text('Continue with Apple'));
    await tester.pumpAndSettle();

    verify(authRepository.signInWithApple()).called(1);
    expect(viewModel.isAuthenticated, isTrue);
  });

  testWidgets('does not show Apple sign-in on Android', (tester) async {
    await tester.pumpWidget(buildScreen(TargetPlatform.android));
    await tester.pumpAndSettle();

    expect(find.text('Continue with Apple'), findsNothing);
    expect(find.text('Continue with Google'), findsOneWidget);
  });
}
