import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';

import '../../core/theme/app_colors.dart';
import '../../core/theme/glass_card.dart';
import '../viewmodels/auth_viewmodel.dart';

/// Shows the in-place "Sign in to continue" dialog for a guest user who taps an
/// action that needs an account (chat send, briefing refresh, etc.)
Future<void> showSignInGateDialog(
  BuildContext context, {
  required AuthViewModel authViewModel,
}) {
  return showDialog<void>(
    context: context,
    builder: (ctx) => _SignInGateDialog(
      onContinueWithApple:
          !kIsWeb && Theme.of(ctx).platform == TargetPlatform.iOS
          ? () async {
              final router = GoRouter.of(ctx);
              Navigator.pop(ctx);
              await authViewModel.signInWithApple();
              if (authViewModel.isAuthenticated) {
                router.go('/home');
              }
            }
          : null,
      onContinueWithGoogle: () async {
        final router = GoRouter.of(ctx);
        Navigator.pop(ctx);
        await authViewModel.signInWithGoogle();
        if (authViewModel.isAuthenticated) {
          router.go('/home');
        }
      },
      onSignInWithEmail: () {
        final router = GoRouter.of(ctx);
        Navigator.pop(ctx);
        router.push('/login');
      },
    ),
  );
}

class _SignInGateDialog extends StatelessWidget {
  final Future<void> Function()? onContinueWithApple;
  final Future<void> Function() onContinueWithGoogle;
  final VoidCallback onSignInWithEmail;

  const _SignInGateDialog({
    required this.onContinueWithApple,
    required this.onContinueWithGoogle,
    required this.onSignInWithEmail,
  });

  @override
  Widget build(BuildContext context) {
    return AlertDialog(
      backgroundColor: AppColors.surface,
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(20),
        side: BorderSide(color: AppColors.glassBorderDim, width: 0.5),
      ),
      title: const Text(
        'Sign in to continue',
        style: TextStyle(
          color: AppColors.textPrimary,
          fontSize: 18,
          fontWeight: FontWeight.w600,
        ),
        textAlign: TextAlign.center,
      ),
      content: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          const Text(
            'Create an account or sign in to chat with Buddy',
            style: TextStyle(color: AppColors.textSecondary, fontSize: 14),
            textAlign: TextAlign.center,
          ),
          const SizedBox(height: 24),
          if (onContinueWithApple != null) ...[
            SizedBox(
              width: double.infinity,
              child: GestureDetector(
                behavior: HitTestBehavior.opaque,
                onTap: onContinueWithApple,
                child: const FauxGlassCard.navTile(
                  child: Center(
                    child: Row(
                      mainAxisSize: MainAxisSize.min,
                      children: [
                        Icon(Icons.apple, color: AppColors.textPrimary),
                        SizedBox(width: 8),
                        Text(
                          'Continue with Apple',
                          style: TextStyle(
                            color: AppColors.textPrimary,
                            fontSize: 15,
                            fontWeight: FontWeight.w600,
                          ),
                        ),
                      ],
                    ),
                  ),
                ),
              ),
            ),
            const SizedBox(height: 12),
          ],
          SizedBox(
            width: double.infinity,
            child: GestureDetector(
              behavior: HitTestBehavior.opaque,
              onTap: onContinueWithGoogle,
              child: const FauxGlassCard.navTile(
                child: Center(
                  child: Text(
                    'Continue with Google',
                    style: TextStyle(
                      color: AppColors.textPrimary,
                      fontSize: 15,
                      fontWeight: FontWeight.w600,
                    ),
                  ),
                ),
              ),
            ),
          ),
          const SizedBox(height: 12),
          SizedBox(
            width: double.infinity,
            child: GestureDetector(
              behavior: HitTestBehavior.opaque,
              onTap: onSignInWithEmail,
              child: const FauxGlassCard.navTile(
                child: Center(
                  child: Text(
                    'Sign in with Email',
                    style: TextStyle(
                      color: AppColors.textPrimary,
                      fontSize: 15,
                      fontWeight: FontWeight.w600,
                    ),
                  ),
                ),
              ),
            ),
          ),
        ],
      ),
    );
  }
}
