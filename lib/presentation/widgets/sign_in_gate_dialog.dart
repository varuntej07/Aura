import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';
import 'package:provider/provider.dart';

import '../../core/theme/app_colors.dart';
import '../../core/theme/glass_card.dart';
import '../viewmodels/auth_viewmodel.dart';

Future<void> showSignInGateDialog(BuildContext context) {
  return showDialog<void>(
    context: context,
    builder: (ctx) => const _SignInGateDialog(),
  );
}

class _SignInGateDialog extends StatelessWidget {
  const _SignInGateDialog();

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
          SizedBox(
            width: double.infinity,
            child: GestureDetector(
              behavior: HitTestBehavior.opaque,
              onTap: () async {
                final router = GoRouter.of(context);
                final authViewModel = context.read<AuthViewModel>();
                Navigator.pop(context);
                await authViewModel.signInWithGoogle();
                if (authViewModel.isAuthenticated) {
                  router.go('/home');
                }
              },
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
              onTap: () {
                Navigator.pop(context);
                context.push('/login');
              },
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
