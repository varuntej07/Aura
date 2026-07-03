import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:qr_flutter/qr_flutter.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:aura/data/services/desktop/overlay_controller.dart'
    show DesktopOnboardingStep;
import 'package:aura/presentation/screens/desktop/desktop_onboarding_flow.dart';

Widget _harness({ValueChanged<DesktopOnboardingStep>? onStepChanged}) {
  return MaterialApp(
    home: Scaffold(
      body: SizedBox(
        width: 520,
        height: 300,
        child: DesktopOnboardingFlow(
          linkStep: const Text('link-step-marker'),
          onStepChanged: onStepChanged,
        ),
      ),
    ),
  );
}

void main() {
  testWidgets('fresh install starts on the welcome step', (tester) async {
    SharedPreferences.setMockInitialValues({});
    await tester.pumpWidget(_harness());
    await tester.pumpAndSettle();

    expect(find.text('Meet Buddy, your AI friend on this PC.'), findsOneWidget);
    expect(find.text('Get set up'), findsOneWidget);
    expect(find.text('link-step-marker'), findsNothing);
  });

  testWidgets('walking the steps: welcome -> QR -> link, and QR encodes /app',
      (tester) async {
    SharedPreferences.setMockInitialValues({});
    await tester.pumpWidget(_harness());
    await tester.pumpAndSettle();

    await tester.tap(find.text('Get set up'));
    await tester.pumpAndSettle();
    expect(find.text('First, grab Aura on your phone'), findsOneWidget);
    expect(find.byType(QrImageView), findsOneWidget);
    expect(find.textContaining('auravoiceapp.com/app'), findsOneWidget);

    await tester.tap(find.text('I have the app'));
    await tester.pumpAndSettle();
    expect(find.text('link-step-marker'), findsOneWidget);
  });

  testWidgets('"Already have Aura? Link now" jumps straight to the link step '
      'and marks onboarding seen', (tester) async {
    SharedPreferences.setMockInitialValues({});
    await tester.pumpWidget(_harness());
    await tester.pumpAndSettle();

    await tester.tap(find.text('Already have Aura? Link now'));
    await tester.pumpAndSettle();

    expect(find.text('link-step-marker'), findsOneWidget);
    final prefs = await SharedPreferences.getInstance();
    expect(prefs.getBool(desktopOnboardingSeenPreferenceKey), isTrue);
  });

  testWidgets('returning user lands directly on the link step with a way back',
      (tester) async {
    SharedPreferences.setMockInitialValues(
        {desktopOnboardingSeenPreferenceKey: true});
    await tester.pumpWidget(_harness());
    await tester.pumpAndSettle();

    expect(find.text('link-step-marker'), findsOneWidget);
    expect(find.text('Meet Buddy, your AI friend on this PC.'), findsNothing);

    await tester.tap(find.text('New here?'));
    await tester.pumpAndSettle();
    expect(find.text('Meet Buddy, your AI friend on this PC.'), findsOneWidget);
  });

  testWidgets('reports each step change so the window can resize to it',
      (tester) async {
    SharedPreferences.setMockInitialValues({});
    final reported = <DesktopOnboardingStep>[];
    await tester.pumpWidget(_harness(onStepChanged: reported.add));
    await tester.pumpAndSettle();
    expect(reported, [DesktopOnboardingStep.welcome]);

    await tester.tap(find.text('Get set up'));
    await tester.pumpAndSettle();
    expect(reported, [
      DesktopOnboardingStep.welcome,
      DesktopOnboardingStep.getApp,
    ]);

    await tester.tap(find.text('I have the app'));
    await tester.pumpAndSettle();
    expect(reported, [
      DesktopOnboardingStep.welcome,
      DesktopOnboardingStep.getApp,
      DesktopOnboardingStep.link,
    ]);
  });
}
