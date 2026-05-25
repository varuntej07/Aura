import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';
import 'package:provider/provider.dart';

import '../../core/theme/app_colors.dart';
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
            child: ElevatedButton.icon(
              onPressed: () {
                Navigator.pop(context);
                context.read<AuthViewModel>().signInWithGoogle();
              },
              icon: const Icon(Icons.g_mobiledata_rounded, size: 24),
              label: const Text('Continue with Google'),
              style: ElevatedButton.styleFrom(
                backgroundColor: AppColors.glassWhiteFill,
                foregroundColor: AppColors.textPrimary,
                padding: const EdgeInsets.symmetric(vertical: 14),
                shape: RoundedRectangleBorder(
                  borderRadius: BorderRadius.circular(12),
                  side: BorderSide(color: AppColors.glassBorderLight, width: 0.5),
                ),
              ),
            ),
          ),
          const SizedBox(height: 12),
          SizedBox(
            width: double.infinity,
            child: ElevatedButton.icon(
              onPressed: () {
                Navigator.pop(context);
                context.push('/login');
              },
              icon: const Icon(Icons.email_outlined, size: 20),
              label: const Text('Sign in with Email'),
              style: ElevatedButton.styleFrom(
                backgroundColor: AppColors.glassWhiteFill,
                foregroundColor: AppColors.textPrimary,
                padding: const EdgeInsets.symmetric(vertical: 14),
                shape: RoundedRectangleBorder(
                  borderRadius: BorderRadius.circular(12),
                  side: BorderSide(color: AppColors.glassBorderLight, width: 0.5),
                ),
              ),
            ),
          ),
        ],
      ),
    );
  }
}
