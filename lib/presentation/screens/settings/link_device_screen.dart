import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../../../core/theme/app_colors.dart';
import '../../../core/theme/glass_card.dart';
import '../../../data/services/backend_api_service.dart';
import '../../../data/services/firebase_auth_service.dart';
import '../../viewmodels/link_device_viewmodel.dart';

/// Phone side of desktop pairing: shows the short-lived code the PC redeems,
/// plus the linked-devices list with unlink.
class LinkDeviceScreen extends StatefulWidget {
  const LinkDeviceScreen({super.key});

  static MaterialPageRoute<void> route() {
    return MaterialPageRoute<void>(
      builder: (_) => const _LinkDeviceScreenProvider(),
    );
  }

  @override
  State<LinkDeviceScreen> createState() => _LinkDeviceScreenState();
}

class _LinkDeviceScreenProvider extends StatelessWidget {
  const _LinkDeviceScreenProvider();

  @override
  Widget build(BuildContext context) {
    return ChangeNotifierProvider(
      create: (ctx) => LinkDeviceViewModel(
        backendApiService: ctx.read<BackendApiService>(),
        userId: ctx.read<FirebaseAuthService>().currentUser?.uid,
      ),
      child: const LinkDeviceScreen(),
    );
  }
}

class _LinkDeviceScreenState extends State<LinkDeviceScreen> {
  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted) return;
      final viewModel = context.read<LinkDeviceViewModel>();
      viewModel.generateCode();
      viewModel.loadLinkedDevices();
    });
  }

  Future<void> _confirmUnlink(LinkedDevice device) async {
    final viewModel = context.read<LinkDeviceViewModel>();
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (dialogContext) => AlertDialog(
        title: Text('Unlink ${device.deviceName}?'),
        content: const Text(
          "For safety this signs you out everywhere, including this phone. "
          "You'll just sign back in.",
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(dialogContext, false),
            child: const Text('Keep it'),
          ),
          FilledButton(
            onPressed: () => Navigator.pop(dialogContext, true),
            child: const Text('Unlink'),
          ),
        ],
      ),
    );
    if (confirmed == true) {
      await viewModel.unlinkDevice(device.id);
    }
  }

  @override
  Widget build(BuildContext context) {
    final viewModel = context.watch<LinkDeviceViewModel>();
    final code = viewModel.formattedCode;
    final remaining = viewModel.remaining;

    return Scaffold(
      backgroundColor: AppColors.background,
      appBar: AppBar(title: const Text('Link this PC')),
      body: SafeArea(
        child: ListView(
          padding: const EdgeInsets.all(20),
          children: [
            Text(
              'On your PC, open Buddy and type this code:',
              style: Theme.of(context).textTheme.bodyMedium?.copyWith(
                    color: AppColors.textPrimary.withValues(alpha: 0.7),
                  ),
            ),
            const SizedBox(height: 16),
            GlassCard(
              padding: const EdgeInsets.symmetric(vertical: 24),
              child: Column(
                children: [
                  if (viewModel.generating)
                    const Padding(
                      padding: EdgeInsets.symmetric(vertical: 8),
                      child: CircularProgressIndicator(strokeWidth: 2),
                    )
                  else if (code != null) ...[
                    Text(
                      code,
                      textAlign: TextAlign.center,
                      style: const TextStyle(
                        fontFamily: 'GeistMono',
                        fontSize: 34,
                        letterSpacing: 6,
                        color: AppColors.textPrimary,
                        fontWeight: FontWeight.w500,
                      ),
                    ),
                    const SizedBox(height: 8),
                    Text(
                      'Expires in ${remaining.inMinutes}:${(remaining.inSeconds % 60).toString().padLeft(2, '0')}',
                      style: const TextStyle(
                          color: AppColors.textTertiary, fontSize: 13),
                    ),
                  ] else ...[
                    const Text(
                      'Code expired',
                      style: TextStyle(
                          color: AppColors.textTertiary, fontSize: 15),
                    ),
                    const SizedBox(height: 12),
                    FilledButton(
                      onPressed: viewModel.generating
                          ? null
                          : () => context
                              .read<LinkDeviceViewModel>()
                              .generateCode(),
                      child: const Text('Get a new code'),
                    ),
                  ],
                ],
              ),
            ),
            const SizedBox(height: 8),
            const Text(
              'Codes are single-use and expire after 5 minutes.',
              style: TextStyle(color: AppColors.textTertiary, fontSize: 12),
            ),
            if (viewModel.error != null) ...[
              const SizedBox(height: 8),
              Text(
                viewModel.error!,
                style: const TextStyle(color: Color(0xFFB3452E), fontSize: 13),
              ),
            ],
            if (viewModel.linkedDevices.isNotEmpty) ...[
              const SizedBox(height: 28),
              Text(
                'Linked devices',
                style: Theme.of(context).textTheme.titleSmall?.copyWith(
                      color: AppColors.textPrimary,
                    ),
              ),
              const SizedBox(height: 8),
              for (final device in viewModel.linkedDevices)
                Padding(
                  padding: const EdgeInsets.only(bottom: 8),
                  child: FauxGlassCard.navTile(
                    child: Row(
                      children: [
                        const Icon(Icons.laptop_windows_outlined,
                            size: 20, color: AppColors.textTertiary),
                        const SizedBox(width: 12),
                        Expanded(
                          child: Text(
                            device.deviceName,
                            style: const TextStyle(
                                color: AppColors.textPrimary, fontSize: 15),
                          ),
                        ),
                        TextButton(
                          onPressed: viewModel.unlinking
                              ? null
                              : () => _confirmUnlink(device),
                          child: const Text('Unlink'),
                        ),
                      ],
                    ),
                  ),
                ),
            ],
          ],
        ),
      ),
    );
  }
}
