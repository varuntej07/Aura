import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:go_router/go_router.dart';
import 'package:provider/provider.dart';
import 'package:url_launcher/url_launcher.dart';

import '../../../core/constants/app_constants.dart';
import '../../../core/theme/app_colors.dart';
import '../../../core/theme/glass_card.dart';
import '../../../data/services/backend_api_service.dart';
import '../../../data/services/firebase_auth_service.dart';
import '../../viewmodels/auth_viewmodel.dart';
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
    if (confirmed != true) return;

    final unlinked = await viewModel.unlinkDevice(device.id);
    if (!unlinked || !mounted) return;

    // The backend just revoked every refresh token for this account, including
    // this phone's own session (exactly what the dialog above promised). Follow
    // through immediately and explicitly instead of leaving it to a confusing,
    // unpredictable auth failure the next time the SDK silently refreshes the
    // ID token.
    await showDialog<void>(
      context: context,
      barrierDismissible: false,
      builder: (dialogContext) => AlertDialog(
        title: const Text('Signed out for safety'),
        content: const Text(
          "You're signed out everywhere, including this phone. Sign back in to keep going.",
        ),
        actions: [
          FilledButton(
            onPressed: () => Navigator.pop(dialogContext),
            child: const Text('OK'),
          ),
        ],
      ),
    );
    if (!mounted) return;
    await context.read<AuthViewModel>().signOut();
    if (!mounted) return;
    // Pushed via Navigator (not GoRouter), so the auth redirect won't clear
    // this screen on its own — navigate to sign-in explicitly.
    context.go('/login');
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
            const _DesktopDownloadBanner(),
            const SizedBox(height: 24),
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
                  if (viewModel.generating) ...[
                    const Padding(
                      padding: EdgeInsets.symmetric(vertical: 8),
                      child: CircularProgressIndicator(strokeWidth: 2),
                    ),
                    if (viewModel.showColdStartHint)
                      const Text(
                        "Waking Buddy's server up, just a moment...",
                        style: TextStyle(
                            color: AppColors.textTertiary, fontSize: 12),
                      ),
                  ] else if (code != null) ...[
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
                        if (viewModel.isUnlinking(device.id))
                          const Padding(
                            padding: EdgeInsets.symmetric(horizontal: 12),
                            child: SizedBox(
                              width: 16,
                              height: 16,
                              child: CircularProgressIndicator(strokeWidth: 2),
                            ),
                          )
                        else
                          TextButton(
                            onPressed: () => _confirmUnlink(device),
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

/// "Get Buddy for Windows" banner shown above the pairing code. The user is on
/// their phone but needs to install on their PC, so copying the link (to paste
/// into the PC's browser) is the primary action; opening it here is secondary.
class _DesktopDownloadBanner extends StatelessWidget {
  const _DesktopDownloadBanner();

  // Scheme stripped for display; the full URL (with https://) is what we copy
  // and launch so it resolves correctly wherever it's pasted.
  static const _displayUrl = 'auravoiceapp.com';

  Future<void> _copyLink(BuildContext context) async {
    await Clipboard.setData(
      const ClipboardData(text: AppConstants.desktopDownloadUrl),
    );
    if (!context.mounted) return;
    ScaffoldMessenger.of(context)
      ..hideCurrentSnackBar()
      ..showSnackBar(
        const SnackBar(
          content: Text("Link copied. Paste it in your PC's browser."),
        ),
      );
  }

  Future<void> _openLink() async {
    await launchUrl(
      Uri.parse(AppConstants.desktopDownloadUrl),
      mode: LaunchMode.externalApplication,
    );
  }

  @override
  Widget build(BuildContext context) {
    return FauxGlassCard.section(
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              const Icon(Icons.laptop_windows_outlined,
                  size: 20, color: AppColors.accentBase),
              const SizedBox(width: 10),
              Text(
                'Buddy for Windows',
                style: Theme.of(context).textTheme.titleSmall?.copyWith(
                      color: AppColors.textPrimary,
                    ),
              ),
            ],
          ),
          const SizedBox(height: 8),
          Text(
            "Don't have the desktop app yet? Get it on your PC, then enter the "
            'code below to link them.',
            style: Theme.of(context).textTheme.bodyMedium?.copyWith(
                  color: AppColors.textPrimary.withValues(alpha: 0.7),
                ),
          ),
          const SizedBox(height: 14),
          Row(
            children: [
              Expanded(
                child: GestureDetector(
                  onTap: () => _copyLink(context),
                  child: Container(
                    padding: const EdgeInsets.symmetric(
                        horizontal: 12, vertical: 10),
                    decoration: BoxDecoration(
                      color: AppColors.glassWhiteFill,
                      borderRadius: BorderRadius.circular(10),
                      border: Border.all(color: AppColors.glassBorderDim),
                    ),
                    child: Row(
                      children: [
                        const Expanded(
                          child: Text(
                            _displayUrl,
                            overflow: TextOverflow.ellipsis,
                            style: TextStyle(
                              fontFamily: 'GeistMono',
                              fontSize: 14,
                              color: AppColors.textPrimary,
                            ),
                          ),
                        ),
                        const SizedBox(width: 8),
                        const Icon(Icons.content_copy_outlined,
                            size: 16, color: AppColors.textTertiary),
                      ],
                    ),
                  ),
                ),
              ),
              const SizedBox(width: 8),
              GlassIconButton(
                icon: Icons.open_in_new,
                onTap: _openLink,
              ),
            ],
          ),
        ],
      ),
    );
  }
}
