import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:aura/presentation/widgets/sign_in_required_view.dart';

void main() {
  testWidgets('renders default title + message and fires onSignIn on tap',
      (tester) async {
    var taps = 0;
    await tester.pumpWidget(
      MaterialApp(
        home: Scaffold(
          body: SignInRequiredView(
            message: 'Sign in to see and manage your reminders.',
            onSignIn: () => taps++,
          ),
        ),
      ),
    );

    expect(find.text('Sign in to continue'), findsOneWidget);
    expect(find.text('Sign in to see and manage your reminders.'),
        findsOneWidget);
    expect(find.text('Sign In'), findsOneWidget);

    await tester.tap(find.text('Sign In'));
    await tester.pump();

    expect(taps, 1);
  });

  testWidgets('uses a custom title when provided', (tester) async {
    await tester.pumpWidget(
      MaterialApp(
        home: Scaffold(
          body: SignInRequiredView(
            title: 'Members only',
            message: 'anything',
            onSignIn: () {},
          ),
        ),
      ),
    );

    expect(find.text('Members only'), findsOneWidget);
  });
}
