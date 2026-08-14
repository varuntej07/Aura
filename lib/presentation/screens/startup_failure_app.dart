import 'package:flutter/material.dart';

import '../../core/theme/app_colors.dart';

/// Shown when a startup dependency failed hard enough that the normal app tree
/// cannot be built.
///
/// The point is that this exists at all. Previously an unrecoverable startup
/// step meant `runApp` was never called, and the user got a window that closed
/// itself with no message, no report, and nothing to describe to support. A
/// visible failure is worth far more than an invisible one: the user can now say
/// what they saw, and the same launch has already filed its forensics.
///
/// Deliberately depends on nothing but Flutter and a colour constant — no
/// providers, no plugins, no network. Anything it needed could be the thing that
/// is broken.
class StartupFailureApp extends StatelessWidget {
  /// The startup step that failed, e.g. `preferences`. Shown to the user so a
  /// support conversation starts with a fact instead of a guess.
  final String failedStep;

  const StartupFailureApp({super.key, required this.failedStep});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      debugShowCheckedModeBanner: false,
      home: Scaffold(
        backgroundColor: AppColors.background,
        body: SafeArea(
          child: Center(
            child: Padding(
              padding: const EdgeInsets.symmetric(horizontal: 32),
              child: Column(
                mainAxisAlignment: MainAxisAlignment.center,
                children: [
                  const Icon(
                    Icons.sentiment_dissatisfied_outlined,
                    size: 48,
                    color: AppColors.textSecondary,
                  ),
                  const SizedBox(height: 24),
                  const Text(
                    "Buddy couldn't start up",
                    textAlign: TextAlign.center,
                    style: TextStyle(
                      fontSize: 20,
                      fontWeight: FontWeight.w600,
                      color: AppColors.textPrimary,
                    ),
                  ),
                  const SizedBox(height: 12),
                  const Text(
                    'Something on this device stopped Buddy from loading. '
                    'Restarting the app usually fixes it. If it keeps '
                    'happening, this has been reported automatically.',
                    textAlign: TextAlign.center,
                    style: TextStyle(
                      fontSize: 15,
                      height: 1.45,
                      color: AppColors.textSecondary,
                    ),
                  ),
                  const SizedBox(height: 28),
                  // The failing step, shown plainly. A user who can read this
                  // aloud gives support more than any amount of guesswork.
                  Text(
                    'Details: $failedStep',
                    textAlign: TextAlign.center,
                    style: TextStyle(
                      fontSize: 13,
                      color: AppColors.textSecondary.withValues(alpha: 0.7),
                    ),
                  ),
                ],
              ),
            ),
          ),
        ),
      ),
    );
  }
}
