/// Widget tests for the onboarding capture screen (`AuraConsentScreen`).
///
/// Closes the coverage gap flagged in the relevance+onboarding eng review: the
/// backend side of onboarding is contract-tested (the picker slug set must equal
/// `ONBOARDABLE_CATEGORIES`, see `backend/tests/test_onboarding_interests_contract.py`)
/// and tap routing is tested, but the capture UI itself was not. These tests pin:
///   - the >=3-interest minimum gate (the user can't continue with fewer),
///   - gender capture (including "Prefer not to say" -> empty, framer stays neutral),
///   - device locale + language capture passed through to the repository,
///   - the minor -> consent-forced-false safeguard,
///   - the save-failure retry UX (snackbar, no navigation).
///
/// Providers (`AuthViewModel`, `OnboardingRepository`) are only read in `_finalize`,
/// so the pure gate/selection tests still exercise the real screen end to end.
library;

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';
import 'package:mockito/annotations.dart';
import 'package:mockito/mockito.dart';
import 'package:provider/provider.dart';

import 'package:aura/data/models/user_model.dart';
import 'package:aura/data/repositories/onboarding_repository.dart';
import 'package:aura/presentation/screens/onboarding/aura_consent_screen.dart';
import 'package:aura/presentation/viewmodels/auth_viewmodel.dart';

import 'aura_consent_screen_test.mocks.dart';

@GenerateNiceMocks([
  MockSpec<AuthViewModel>(),
  MockSpec<OnboardingRepository>(),
])
UserModel _user({String uid = 'uid-test'}) => UserModel(
      uid: uid,
      displayName: 'Test User',
      email: 'test@example.com',
      settings: UserSettings.defaults(),
      createdAt: DateTime(2026, 1, 1),
      lastActiveAt: DateTime(2026, 1, 1),
      onboardingComplete: false,
      auraConsentGranted: null,
    );

/// Onboarding screens are tall; give the test a phone-sized surface (dpr 1.0 so
/// logical == physical) so a fixed-height step never reports a RenderFlex overflow.
void _useTallSurface(WidgetTester tester) {
  tester.view.physicalSize = const Size(1080, 2400);
  tester.view.devicePixelRatio = 1.0;
  addTearDown(() {
    tester.view.resetPhysicalSize();
    tester.view.resetDevicePixelRatio();
  });
}

/// Wrap the screen in the providers it reads at finalize time, behind a minimal
/// router so a successful save's `context.go('/home')` has somewhere to land.
Widget _harness({
  required AuthViewModel authVm,
  required OnboardingRepository repo,
}) {
  final router = GoRouter(
    routes: [
      GoRoute(path: '/', builder: (_, _) => const AuraConsentScreen()),
      GoRoute(
        path: '/home',
        builder: (_, _) => const Scaffold(body: Center(child: Text('HOME'))),
      ),
    ],
  );
  return MultiProvider(
    providers: [
      ChangeNotifierProvider<AuthViewModel>.value(value: authVm),
      Provider<OnboardingRepository>.value(value: repo),
    ],
    child: MaterialApp.router(routerConfig: router),
  );
}

/// Step 0 (name) -> 1. Types a name so the non-empty gate passes, then advances.
Future<void> _passNameStep(WidgetTester tester, {String name = 'Lency'}) async {
  await tester.enterText(find.byType(TextField), name);
  await tester.tap(find.text('Continue'));
  await tester.pumpAndSettle();
}

/// Step 1 (age) -> 2. The default DOB is exactly 13 years ago (the picker's
/// maximum), so the age check passes without touching the wheel.
Future<void> _passAgeGate(WidgetTester tester) async {
  await tester.tap(find.text('Continue'));
  await tester.pumpAndSettle();
}

Future<void> _tapChips(WidgetTester tester, List<String> labels) async {
  for (final label in labels) {
    await tester.tap(find.text(label));
    await tester.pump();
  }
}

/// Stub every named arg of saveOnboardingResult and return [success].
void _stubSave(MockOnboardingRepository repo, {required bool success}) {
  when(repo.saveOnboardingResult(
    uid: anyNamed('uid'),
    displayName: anyNamed('displayName'),
    dateOfBirth: anyNamed('dateOfBirth'),
    auraConsentGranted: anyNamed('auraConsentGranted'),
    gender: anyNamed('gender'),
    interestSlugs: anyNamed('interestSlugs'),
    locale: anyNamed('locale'),
    language: anyNamed('language'),
  )).thenAnswer((_) async => success);
}

/// Captured args of the single saveOnboardingResult call, in declared order:
/// [uid, dateOfBirth, auraConsentGranted, gender, interestSlugs, locale,
/// language, displayName]. displayName is captured last so the existing
/// positional expectations stay stable.
List<dynamic> _capturedSave(MockOnboardingRepository repo) => verify(
      repo.saveOnboardingResult(
        uid: captureAnyNamed('uid'),
        dateOfBirth: captureAnyNamed('dateOfBirth'),
        auraConsentGranted: captureAnyNamed('auraConsentGranted'),
        gender: captureAnyNamed('gender'),
        interestSlugs: captureAnyNamed('interestSlugs'),
        locale: captureAnyNamed('locale'),
        language: captureAnyNamed('language'),
        displayName: captureAnyNamed('displayName'),
      ),
    ).captured;

void main() {
  testWidgets('blocks advancing with fewer than 3 interests', (tester) async {
    _useTallSurface(tester);
    await tester.pumpWidget(
      _harness(authVm: MockAuthViewModel(), repo: MockOnboardingRepository()),
    );
    await tester.pumpAndSettle();
    await _passNameStep(tester);
    await _passAgeGate(tester);

    // Only 2 of the required 3.
    await _tapChips(tester, ['Sports', 'Technology']);
    await tester.tap(find.text('Continue'));
    await tester.pumpAndSettle();

    expect(find.textContaining('Pick at least 3'), findsOneWidget);
    // Still on the profile step — never reached the consent step.
    expect(find.text('Start using Buddy'), findsNothing);
  });

  testWidgets('advances to the consent step once 3 interests are picked',
      (tester) async {
    _useTallSurface(tester);
    await tester.pumpWidget(
      _harness(authVm: MockAuthViewModel(), repo: MockOnboardingRepository()),
    );
    await tester.pumpAndSettle();
    await _passNameStep(tester);
    await _passAgeGate(tester);

    await _tapChips(tester, ['Sports', 'Technology', 'News']);
    await tester.tap(find.text('Continue'));
    await tester.pumpAndSettle();

    expect(find.text('Start using Buddy'), findsOneWidget);
  });

  testWidgets(
      'captures gender, interests, locale and language, forces minor consent off, '
      'and navigates home on success', (tester) async {
    _useTallSurface(tester);
    // A Telugu / India device: exercises the de-bias keys end to end. `.locale`
    // (what the screen reads) is driven by the singular localeTestValue, not the
    // plural localesTestValue (which only overrides `.locales`).
    tester.platformDispatcher.localeTestValue = const Locale('te', 'IN');
    addTearDown(tester.platformDispatcher.clearLocaleTestValue);

    final authVm = MockAuthViewModel();
    when(authVm.user).thenReturn(_user(uid: 'uid-xyz'));
    final repo = MockOnboardingRepository();
    _stubSave(repo, success: true);

    await tester.pumpWidget(_harness(authVm: authVm, repo: repo));
    await tester.pumpAndSettle();
    await _passNameStep(tester, name: 'Lency');
    await _passAgeGate(tester);

    await _tapChips(tester, ['Male', 'Sports', 'Technology', 'News']);
    await tester.tap(find.text('Continue'));
    await tester.pumpAndSettle();

    await tester.tap(find.text('Start using Buddy'));
    await tester.pumpAndSettle();

    final captured = _capturedSave(repo);
    expect(captured[0], 'uid-xyz'); // uid
    expect(captured[3], 'male'); // gender (tone only)
    expect(
      captured[4],
      containsAll(<String>['sports', 'technology_computing', 'news_current_affairs']),
    ); // interestSlugs
    expect((captured[4] as List).length, 3);
    expect(captured[5], 'te-IN'); // locale
    expect(captured[6], 'Telugu'); // language
    expect(captured[7], 'Lency'); // displayName (captured last)
    // Default picker DOB is exactly 13 years ago => a minor => consent forced off
    // regardless of the toggle (GDPR safeguard).
    expect(captured[2], isFalse); // auraConsentGranted

    verify(authVm.markOnboardingComplete(
      auraConsentGranted: false,
      displayName: 'Lency',
    )).called(1);
    expect(find.text('HOME'), findsOneWidget);
  });

  testWidgets('"Prefer not to say" stores an empty gender so the framer stays neutral',
      (tester) async {
    _useTallSurface(tester);
    final authVm = MockAuthViewModel();
    when(authVm.user).thenReturn(_user());
    final repo = MockOnboardingRepository();
    _stubSave(repo, success: true);

    await tester.pumpWidget(_harness(authVm: authVm, repo: repo));
    await tester.pumpAndSettle();
    await _passNameStep(tester);
    await _passAgeGate(tester);

    await _tapChips(
      tester,
      ['Prefer not to say', 'Sports', 'Technology', 'News'],
    );
    await tester.tap(find.text('Continue'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Start using Buddy'));
    await tester.pumpAndSettle();

    final captured = _capturedSave(repo);
    expect(captured[3], ''); // explicit "prefer not to say" -> empty, never a guess
  });

  testWidgets('shows a retry snackbar and does not navigate when the save fails',
      (tester) async {
    _useTallSurface(tester);
    final authVm = MockAuthViewModel();
    when(authVm.user).thenReturn(_user());
    final repo = MockOnboardingRepository();
    _stubSave(repo, success: false);

    await tester.pumpWidget(_harness(authVm: authVm, repo: repo));
    await tester.pumpAndSettle();
    await _passNameStep(tester);
    await _passAgeGate(tester);

    await _tapChips(tester, ['Sports', 'Technology', 'News']);
    await tester.tap(find.text('Continue'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Start using Buddy'));
    await tester.pumpAndSettle();

    expect(find.text('Something went wrong. Please try again.'), findsOneWidget);
    expect(find.text('HOME'), findsNothing);
    verifyNever(
      authVm.markOnboardingComplete(
        auraConsentGranted: anyNamed('auraConsentGranted'),
        displayName: anyNamed('displayName'),
      ),
    );
  });

  testWidgets('blocks advancing past the name step when the field is empty',
      (tester) async {
    _useTallSurface(tester);
    await tester.pumpWidget(
      _harness(authVm: MockAuthViewModel(), repo: MockOnboardingRepository()),
    );
    await tester.pumpAndSettle();

    // Field starts blank (no provider name on the nice mock). Clear it to be
    // explicit, then try to advance.
    await tester.enterText(find.byType(TextField), '   ');
    await tester.tap(find.text('Continue'));
    await tester.pumpAndSettle();

    expect(find.textContaining('needs something to call you'), findsOneWidget);
    // Still on the name step — the age heading never appeared.
    expect(find.text('Quick age check'), findsNothing);
  });

  testWidgets('pre-fills the name field with a friendly form of the provider name',
      (tester) async {
    _useTallSurface(tester);
    final authVm = MockAuthViewModel();
    // Raw Google name: full, upper-cased, multi-token.
    when(authVm.user).thenReturn(_user().copyWith(displayName: 'LENCY C D'));
    final repo = MockOnboardingRepository();
    _stubSave(repo, success: true);

    await tester.pumpWidget(_harness(authVm: authVm, repo: repo));
    await tester.pumpAndSettle();

    // "LENCY C D" -> "Lency": first token, Title-cased.
    expect(find.widgetWithText(TextField, 'Lency'), findsOneWidget);
  });
}
