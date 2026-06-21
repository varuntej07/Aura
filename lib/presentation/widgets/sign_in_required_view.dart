import 'package:flutter/material.dart';

import '../../core/theme/app_colors.dart';

/// Full-screen empty state shown when a logged-out (guest) user reaches an
/// auth-gated screen. Instead of a dead-end message, it explains what needs an
/// account and offers a primary action that routes to sign-in. The router sends
/// the user on to home/onboarding once they authenticate.
///
/// Presentational only (per the Widget Purity rule): navigation comes in via
/// [onSignIn]. It centers itself and provides no Scaffold, so place it inside a
/// screen body. Mirror screen of the shared [showSignInGateDialog] for the
/// in-place actions (chat send, etc.) that use a dialog instead of a full page.
class SignInRequiredView extends StatelessWidget {
  /// One-line reason this screen needs an account, e.g.
  /// "Sign in to see and manage your reminders."
  final String message;

  /// Headline above [message]. Defaults to a friendly, generic prompt.
  final String title;

  /// Leading glyph shown in the accent circle. Defaults to a lock.
  final IconData icon;

  /// Invoked when the user taps the primary button. The caller owns navigation
  /// (typically `() => context.go('/login')`).
  final VoidCallback onSignIn;

  const SignInRequiredView({
    super.key,
    required this.message,
    required this.onSignIn,
    this.title = 'Sign in to continue',
    this.icon = Icons.lock_outline_rounded,
  });

  @override
  Widget build(BuildContext context) {
    return Center(
      child: SingleChildScrollView(
        padding: const EdgeInsets.symmetric(horizontal: 32, vertical: 24),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Container(
              width: 72,
              height: 72,
              decoration: BoxDecoration(
                color: AppColors.accent.withValues(alpha: 0.08),
                shape: BoxShape.circle,
              ),
              child: Icon(icon, size: 32, color: AppColors.accent),
            ),
            const SizedBox(height: 20),
            Text(
              title,
              textAlign: TextAlign.center,
              style: const TextStyle(
                color: AppColors.textPrimary,
                fontSize: 20,
                fontWeight: FontWeight.w700,
                letterSpacing: -0.4,
              ),
            ),
            const SizedBox(height: 10),
            Text(
              message,
              textAlign: TextAlign.center,
              style: const TextStyle(
                color: AppColors.textSecondary,
                fontSize: 14,
                height: 1.5,
              ),
            ),
            const SizedBox(height: 24),
            GestureDetector(
              onTap: onSignIn,
              child: Container(
                height: 52,
                padding: const EdgeInsets.symmetric(horizontal: 28),
                decoration: BoxDecoration(
                  gradient: LinearGradient(
                    begin: Alignment.topLeft,
                    end: Alignment.bottomRight,
                    colors: [AppColors.accent, AppColors.accentDark],
                  ),
                  borderRadius: BorderRadius.circular(16),
                ),
                child: const Row(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    Icon(Icons.login_rounded, color: Colors.black, size: 18),
                    SizedBox(width: 8),
                    Text(
                      'Sign In',
                      style: TextStyle(
                        color: Colors.black,
                        fontSize: 16,
                        fontWeight: FontWeight.w600,
                        letterSpacing: -0.2,
                      ),
                    ),
                  ],
                ),
              ),
            ),
          ],
        ),
      ),
    );
  }
}
