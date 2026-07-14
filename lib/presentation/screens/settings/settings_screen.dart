import 'dart:io' show Platform;

import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';
import 'package:provider/provider.dart';
import 'package:url_launcher/url_launcher.dart';

import '../../../core/theme/app_colors.dart';
import '../../../core/theme/glass_card.dart';
import '../../../data/services/voice_launcher_bridge.dart';
import '../../viewmodels/auth_viewmodel.dart';
import '../../viewmodels/settings_viewmodel.dart';
import '../../widgets/error_display.dart';
import '../../widgets/loading_indicator.dart';
import '../connectors/connectors_screen.dart';
import '../onboarding/aura_consent_screen.dart';
import '../reminders/reminders_screen.dart';
import 'aura_profile_screen.dart';
import 'link_device_screen.dart';

class SettingsScreen extends StatefulWidget {
  const SettingsScreen({super.key});

  @override
  State<SettingsScreen> createState() => _SettingsScreenState();
}

class _SettingsScreenState extends State<SettingsScreen> {
  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addPostFrameCallback((_) {
      final user = context.read<AuthViewModel>().user;
      if (user != null) {
        context.read<SettingsViewModel>().loadUser(user);
      }
    });
  }

  Future<void> _signOut(BuildContext context) async {
    final authVm = context.read<AuthViewModel>();
    await authVm.signOut();
    if (!context.mounted) return;
    // Settings was pushed via Navigator (not GoRouter), so the redirect won't
    // clear this screen on its own — navigating to the sign-in screen explicitly.
    context.go('/login');
  }

  Future<void> _confirmDeleteAccount(BuildContext context) async {
    final authVm = context.read<AuthViewModel>();
    final messenger = ScaffoldMessenger.of(context);
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        backgroundColor: AppColors.deepBackground,
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(20)),
        title: const Text(
          'Delete account?',
          style: TextStyle(
            color: AppColors.textPrimary,
            fontWeight: FontWeight.w700,
          ),
        ),
        content: const Text(
          'All your data (chats, reminders, and your Aura profile) will be permanently deleted. This cannot be undone.',
          style: TextStyle(color: AppColors.textSecondary, height: 1.5),
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(ctx, false),
            child: const Text(
              'Cancel',
              style: TextStyle(color: AppColors.textTertiary),
            ),
          ),
          TextButton(
            onPressed: () => Navigator.pop(ctx, true),
            child: const Text(
              'Delete forever',
              style: TextStyle(
                color: AppColors.error,
                fontWeight: FontWeight.w600,
              ),
            ),
          ),
        ],
      ),
    );

    if (confirmed != true) return;
    if (!mounted) return;

    final errorMessage = await authVm.deleteAccount();
    if (!mounted) return;
    if (errorMessage != null) {
      messenger.showSnackBar(
        SnackBar(content: Text(errorMessage), backgroundColor: AppColors.error),
      );
    }
  }

  /// Pins the one-tap voice widget to the home screen. The launcher shows its own
  /// placement confirmation when it supports app-initiated pinning; otherwise we
  /// point the user at the manual widget tray.
  Future<void> _addVoiceWidget(BuildContext context) async {
    final messenger = ScaffoldMessenger.of(context);
    final requested = await VoiceLauncherBridge.instance
        .requestPinVoiceWidget();
    if (!mounted) return;
    messenger.showSnackBar(
      SnackBar(
        content: Text(
          requested
              ? 'Check your home screen to drop the "Talk to Buddy" widget.'
              : "Your launcher can't add it from here. Long-press your home "
                    'screen, tap Widgets, and pick Aura.',
          style: const TextStyle(color: AppColors.textPrimary),
        ),
        backgroundColor: AppColors.surfaceVariant,
        behavior: SnackBarBehavior.floating,
      ),
    );
  }

  /// Toggles Aura memory consent. Turning ON opens the age-gated consent screen
  /// (informed consent + the under-18 rule live there, never a bare write).
  /// Turning OFF is the GDPR withdrawal: confirm, then write consent = false,
  /// which stops Buddy learning and stops the stored profile being used.
  Future<void> _onToggleAuraMemory(BuildContext context, bool enable) async {
    final authVm = context.read<AuthViewModel>();

    if (enable) {
      await Navigator.of(context).push(
        MaterialPageRoute<void>(builder: (_) => const AuraConsentScreen()),
      );
      return;
    }

    final messenger = ScaffoldMessenger.of(context);
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        backgroundColor: AppColors.deepBackground,
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(20)),
        title: const Text(
          'Turn off Aura memory?',
          style: TextStyle(
            color: AppColors.textPrimary,
            fontWeight: FontWeight.w700,
          ),
        ),
        content: const Text(
          'Buddy will stop learning from your conversations, and what it has '
          'already learned will no longer be used to personalize your chats or '
          'notifications. You can turn it back on anytime. To erase what Buddy '
          'has learned, delete your account.',
          style: TextStyle(color: AppColors.textSecondary, height: 1.5),
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(ctx, false),
            child: const Text(
              'Cancel',
              style: TextStyle(color: AppColors.textTertiary),
            ),
          ),
          TextButton(
            onPressed: () => Navigator.pop(ctx, true),
            child: const Text(
              'Turn off',
              style: TextStyle(
                color: AppColors.error,
                fontWeight: FontWeight.w600,
              ),
            ),
          ),
        ],
      ),
    );

    if (confirmed != true) return;
    if (!context.mounted) return;

    final ok = await authVm.revokeAuraMemory();
    if (!context.mounted) return;
    if (!ok) {
      messenger.showSnackBar(
        const SnackBar(
          content: Text('Something went wrong. Try again in a moment.'),
          backgroundColor: AppColors.error,
        ),
      );
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: Colors.transparent,
      body: AmbientBackground(
        child: SafeArea(
          child: Column(
            children: [
              // Top bar
              Padding(
                padding: const EdgeInsets.symmetric(
                  horizontal: 20,
                  vertical: 12,
                ),
                child: Row(
                  children: [
                    GlassIconButton(
                      icon: Icons.arrow_back_ios_new,
                      onTap: () => Navigator.pop(context),
                      iconSize: 17,
                    ),
                    const SizedBox(width: 14),
                    const Text(
                      'Settings',
                      style: TextStyle(
                        color: AppColors.textPrimary,
                        fontSize: 20,
                        fontWeight: FontWeight.w700,
                        letterSpacing: -0.5,
                      ),
                    ),
                  ],
                ),
              ),

              // Body
              Expanded(
                child: Consumer2<SettingsViewModel, AuthViewModel>(
                  builder: (context, settingsVm, authVm, _) {
                    if (settingsVm.state == ViewState.loading) {
                      return const FullScreenLoader();
                    }

                    final settings = settingsVm.settings;
                    final user = authVm.user;

                    return ListView(
                      padding: const EdgeInsets.fromLTRB(16, 4, 16, 32),
                      children: [
                        if (settingsVm.error != null)
                          Padding(
                            padding: const EdgeInsets.only(bottom: 12),
                            child: ErrorDisplay(
                              error: settingsVm.error!,
                              onDismiss: settingsVm.clearError,
                            ),
                          ),

                        // ── Voice ───────────────────────────────────────────
                        _SectionLabel('Voice'),
                        _GlassToggleTile(
                          icon: Icons.record_voice_over_rounded,
                          title: 'Wake Word',
                          subtitle: 'Activate with "Hey Buddy"',
                          value: settings?.wakeWordEnabled ?? false,
                          onChanged: settingsVm.toggleWakeWord,
                        ),
                        const SizedBox(height: 8),
                        _GlassToggleTile(
                          icon: Icons.volume_up_rounded,
                          title: 'Voice Responses',
                          subtitle: 'Read responses aloud (TTS)',
                          value: settings?.ttsEnabled ?? true,
                          onChanged: settingsVm.toggleTts,
                        ),
                        // Home-screen widget: one tap opens the app with mic on.
                        // Android-only (iOS WidgetKit ships separately).
                        if (Platform.isAndroid) ...[
                          const SizedBox(height: 8),
                          _GlassNavTile(
                            icon: Icons.add_to_home_screen_rounded,
                            title: 'Add to home screen',
                            subtitle:
                                'One-tap widget that opens Buddy with the mic on',
                            onTap: () => _addVoiceWidget(context),
                          ),
                        ],

                        // ── Buddy on your PC ─────────────────────────────────
                        _SectionLabel('Buddy on your PC'),
                        _GlassNavTile(
                          icon: Icons.desktop_windows_rounded,
                          title: 'Link this PC',
                          subtitle:
                              'Get a code to sign in Buddy on your desktop',
                          onTap: () =>
                              Navigator.push(context, LinkDeviceScreen.route()),
                        ),

                        // ── Reminders ────────────────────────────────────────
                        _SectionLabel('Reminders'),
                        _GlassNavTile(
                          icon: Icons.notifications_active_rounded,
                          title: 'View Reminders',
                          subtitle: 'See all scheduled reminders',
                          onTap: () =>
                              Navigator.push(context, RemindersScreen.route()),
                        ),

                        _SectionLabel('Connectors'),
                        _GlassNavTile(
                          icon: Icons.link_rounded,
                          title: 'Connectors',
                          subtitle: 'Calendar, Gmail & more',
                          onTap: () => Navigator.push(
                            context,
                            MaterialPageRoute<void>(
                              builder: (_) => const ConnectorsScreen(),
                            ),
                          ),
                        ),

                        // Aura memory — consent toggle + profile
                        _SectionLabel('Aura Memory'),
                        _GlassToggleTile(
                          icon: Icons.memory_rounded,
                          title: 'Aura memory',
                          subtitle: authVm.auraMemoryEnabled
                              ? 'Buddy learns from your chats to personalize everything'
                              : 'Turn on to let Buddy remember what matters to you',
                          value: authVm.auraMemoryEnabled,
                          onChanged: (enabled) =>
                              _onToggleAuraMemory(context, enabled),
                        ),
                        const SizedBox(height: 8),
                        _GlassNavTile(
                          icon: Icons.psychology_alt_rounded,
                          title: 'Your Aura Profile',
                          subtitle: 'See what Buddy has learned about you',
                          onTap: () => Navigator.push(
                            context,
                            AuraProfileScreen.route(),
                          ),
                        ),

                        // Subscription
                        _SectionLabel('Subscription'),
                        _GlassNavTile(
                          icon: Icons.workspace_premium_rounded,
                          title: 'Upgrade Plan',
                          subtitle: 'View plans and manage subscription',
                          onTap: () => context.push('/paywall'),
                        ),

                        // Account
                        _SectionLabel('Account'),
                        if (user != null) ...[
                          _GlassInfoTile(
                            icon: Icons.person_rounded,
                            label: 'Name',
                            value: user.displayName,
                          ),
                          const SizedBox(height: 8),
                          _GlassInfoTile(
                            icon: Icons.alternate_email_rounded,
                            label: 'Email',
                            value: user.email,
                          ),
                        ],

                        // ── Feedback ─────────────────────────────────────────
                        _SectionLabel('Feedback'),
                        _GlassNavTile(
                          icon: Icons.rate_review_rounded,
                          title: 'Send Feedback',
                          subtitle: 'Tell us what to change or fix',
                          onTap: () => showFeedbackSheet(context),
                        ),

                        // ── Legal ────────────────────────────────────────────
                        _SectionLabel('Legal'),
                        _GlassNavTile(
                          icon: Icons.admin_panel_settings_rounded,
                          title: 'Privacy Policy',
                          subtitle: 'How we handle your data',
                          onTap: () => launchUrl(
                            Uri.parse(
                              'https://auravoiceapp.com/privacy-policy',
                            ),
                            mode: LaunchMode.externalApplication,
                          ),
                        ),
                        const SizedBox(height: 8),
                        _GlassNavTile(
                          icon: Icons.article_rounded,
                          title: 'Terms of Service',
                          subtitle: 'Terms and conditions',
                          onTap: () => launchUrl(
                            Uri.parse(
                              'https://auravoiceapp.com/terms-of-service',
                            ),
                            mode: LaunchMode.externalApplication,
                          ),
                        ),

                        const SizedBox(height: 28),
                        // Sign Out / Delete only make sense for a real session.
                        // A logged-out guest who lands here gets a Sign In button
                        // instead, never a dead "Sign Out".
                        if (user != null) ...[
                          _GlassSignOutButton(onTap: () => _signOut(context)),
                          const SizedBox(height: 12),
                          _GlassDeleteAccountButton(
                            onTap: () => _confirmDeleteAccount(context),
                          ),
                        ] else
                          _GlassSignInButton(onTap: () => context.go('/login')),

                        const SizedBox(height: 28),
                        Center(
                          child: Text(
                            'Aura v2.2.0',
                            style: const TextStyle(
                              color: AppColors.textTertiary,
                              fontSize: 12,
                            ),
                          ),
                        ),
                      ],
                    );
                  },
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}

/// Opens the beta feedback bottom sheet and shows an acknowledgement on success.
/// Shared by the Settings screen and the home drawer's "Help & feedback" row.
Future<void> showFeedbackSheet(BuildContext context) async {
  final settingsVm = context.read<SettingsViewModel>();
  // The drawer can open this before Settings has ever run loadUser, so seed the
  // user from auth when needed — submitFeedback requires it.
  if (settingsVm.user == null) {
    final authUser = context.read<AuthViewModel>().user;
    if (authUser != null) settingsVm.loadUser(authUser);
  }
  final messenger = ScaffoldMessenger.of(context);
  final sent = await showModalBottomSheet<bool>(
    context: context,
    isScrollControlled: true,
    backgroundColor: Colors.transparent,
    builder: (_) => _FeedbackSheet(
      onSubmit: (text, category) =>
          settingsVm.submitFeedback(text: text, category: category),
    ),
  );
  if (sent == true && context.mounted) {
    messenger.showSnackBar(
      const SnackBar(
        content: Text(
          'Got it, thanks for the feedback.',
          style: TextStyle(color: AppColors.textPrimary),
        ),
        backgroundColor: AppColors.surfaceVariant,
      ),
    );
  }
}

// Delete account button

class _GlassDeleteAccountButton extends StatelessWidget {
  final VoidCallback onTap;
  const _GlassDeleteAccountButton({required this.onTap});

  @override
  Widget build(BuildContext context) {
    return GestureDetector(
      onTap: onTap,
      child: FauxGlassCard(
        borderRadius: 30,
        padding: const EdgeInsets.symmetric(vertical: 16),
        borderColor: AppColors.error.withValues(alpha: 0.15),
        gradient: LinearGradient(
          begin: Alignment.topLeft,
          end: Alignment.bottomRight,
          colors: [
            AppColors.error.withValues(alpha: 0.05),
            AppColors.error.withValues(alpha: 0.02),
          ],
        ),
        child: const Center(
          child: Text(
            'Delete Account',
            style: TextStyle(
              color: AppColors.error,
              fontSize: 15,
              fontWeight: FontWeight.w500,
            ),
          ),
        ),
      ),
    );
  }
}

// Section label

class _SectionLabel extends StatelessWidget {
  final String title;
  const _SectionLabel(this.title);

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.fromLTRB(4, 24, 4, 8),
      child: Text(
        title.toUpperCase(),
        style: const TextStyle(
          color: AppColors.textTertiary,
          fontSize: 11,
          fontWeight: FontWeight.w600,
          letterSpacing: 1.2,
        ),
      ),
    );
  }
}

// Toggle tile

class _GlassToggleTile extends StatelessWidget {
  final IconData icon;
  final String title;
  final String subtitle;
  final bool value;
  final ValueChanged<bool> onChanged;

  const _GlassToggleTile({
    required this.icon,
    required this.title,
    required this.subtitle,
    required this.value,
    required this.onChanged,
  });

  @override
  Widget build(BuildContext context) {
    return FauxGlassCard.toggleTile(
      child: SwitchListTile(
        secondary: _SettingsIcon(icon),
        title: Text(
          title,
          style: const TextStyle(color: AppColors.textPrimary, fontSize: 15),
        ),
        subtitle: Text(
          subtitle,
          style: const TextStyle(color: AppColors.textTertiary, fontSize: 13),
        ),
        value: value,
        onChanged: onChanged,
        activeThumbColor: AppColors.accent,
        activeTrackColor: AppColors.accent.withValues(alpha: 0.3),
      ),
    );
  }
}

// Nav tile

class _GlassNavTile extends StatelessWidget {
  final IconData icon;
  final String title;
  final String subtitle;
  final VoidCallback onTap;

  const _GlassNavTile({
    required this.icon,
    required this.title,
    required this.subtitle,
    required this.onTap,
  });

  @override
  Widget build(BuildContext context) {
    return GestureDetector(
      onTap: onTap,
      child: FauxGlassCard.navTile(
        child: Row(
          children: [
            _SettingsIcon(icon),
            const SizedBox(width: 12),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    title,
                    style: const TextStyle(
                      color: AppColors.textPrimary,
                      fontSize: 15,
                    ),
                  ),
                  const SizedBox(height: 2),
                  Text(
                    subtitle,
                    style: const TextStyle(
                      color: AppColors.textTertiary,
                      fontSize: 13,
                    ),
                  ),
                ],
              ),
            ),
            const Icon(
              Icons.chevron_right,
              size: 18,
              color: AppColors.textTertiary,
            ),
          ],
        ),
      ),
    );
  }
}

class _SettingsIcon extends StatelessWidget {
  final IconData icon;

  const _SettingsIcon(this.icon);

  @override
  Widget build(BuildContext context) {
    return Container(
      width: 40,
      height: 40,
      decoration: BoxDecoration(
        color: AppColors.accent.withValues(alpha: 0.16),
        borderRadius: BorderRadius.circular(12),
        border: Border.all(color: AppColors.accent.withValues(alpha: 0.18)),
      ),
      child: Icon(icon, size: 20, color: AppColors.accentDark),
    );
  }
}

// Info tile

class _GlassInfoTile extends StatelessWidget {
  final IconData icon;
  final String label;
  final String value;

  const _GlassInfoTile({
    required this.icon,
    required this.label,
    required this.value,
  });

  @override
  Widget build(BuildContext context) {
    return FauxGlassCard.navTile(
      child: Row(
        children: [
          _SettingsIcon(icon),
          const SizedBox(width: 12),
          Text(
            label,
            style: const TextStyle(color: AppColors.textTertiary, fontSize: 14),
          ),
          const Spacer(),
          Flexible(
            child: Text(
              value,
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
              textAlign: TextAlign.end,
              style: const TextStyle(
                color: AppColors.textPrimary,
                fontSize: 14,
              ),
            ),
          ),
        ],
      ),
    );
  }
}

// Sign-in button — shown in place of Sign Out / Delete when logged out, so a
// guest who lands in Settings has a clear, accent-styled way back in.

class _GlassSignInButton extends StatelessWidget {
  final VoidCallback onTap;
  const _GlassSignInButton({required this.onTap});

  @override
  Widget build(BuildContext context) {
    return GestureDetector(
      onTap: onTap,
      child: FauxGlassCard(
        borderRadius: 16,
        padding: const EdgeInsets.symmetric(vertical: 16),
        borderColor: AppColors.accent.withValues(alpha: 0.4),
        gradient: LinearGradient(
          begin: Alignment.topLeft,
          end: Alignment.bottomRight,
          colors: [
            AppColors.accent.withValues(alpha: 0.16),
            AppColors.accent.withValues(alpha: 0.07),
          ],
        ),
        child: Row(
          mainAxisAlignment: MainAxisAlignment.center,
          children: [
            Icon(Icons.login_rounded, color: AppColors.accentDark, size: 18),
            const SizedBox(width: 8),
            Text(
              'Sign In',
              style: TextStyle(
                color: AppColors.accentDark,
                fontSize: 16,
                fontWeight: FontWeight.w600,
              ),
            ),
          ],
        ),
      ),
    );
  }
}

// Sign-out button

class _GlassSignOutButton extends StatelessWidget {
  final VoidCallback onTap;
  const _GlassSignOutButton({required this.onTap});

  @override
  Widget build(BuildContext context) {
    return GestureDetector(
      onTap: onTap,
      child: FauxGlassCard.destructiveButton(
        child: const Center(
          child: Text(
            'Sign Out',
            style: TextStyle(
              color: AppColors.error,
              fontSize: 16,
              fontWeight: FontWeight.w600,
            ),
          ),
        ),
      ),
    );
  }
}

// Feedback sheet

/// The selectable feedback categories. Label is shown on the chip; value is the
/// string stored in Firestore and sent to PostHog.
const List<({String label, String value})> _feedbackCategories = [
  (label: 'Idea', value: 'idea'),
  (label: 'Bug', value: 'bug'),
  (label: 'Voice', value: 'voice'),
  (label: 'Other', value: 'other'),
];

class _FeedbackSheet extends StatefulWidget {
  /// Returns null on success, or a user-facing error message on failure.
  final Future<String?> Function(String text, String category) onSubmit;

  const _FeedbackSheet({required this.onSubmit});

  @override
  State<_FeedbackSheet> createState() => _FeedbackSheetState();
}

class _FeedbackSheetState extends State<_FeedbackSheet> {
  final TextEditingController _controller = TextEditingController();
  String _selectedCategory = _feedbackCategories.first.value;
  bool _isSubmitting = false;
  bool _hasText = false;
  String? _errorMessage;

  @override
  void initState() {
    super.initState();
    _controller.addListener(() {
      final hasText = _controller.text.trim().isNotEmpty;
      if (hasText != _hasText) setState(() => _hasText = hasText);
    });
  }

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  Future<void> _submit() async {
    setState(() {
      _isSubmitting = true;
      _errorMessage = null;
    });
    final error = await widget.onSubmit(_controller.text, _selectedCategory);
    if (!mounted) return;
    if (error == null) {
      Navigator.pop(context, true);
      return;
    }
    setState(() {
      _isSubmitting = false;
      _errorMessage = error;
    });
  }

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: EdgeInsets.only(
        bottom: MediaQuery.of(context).viewInsets.bottom,
      ),
      child: Container(
        decoration: const BoxDecoration(
          color: AppColors.deepBackground,
          borderRadius: BorderRadius.vertical(top: Radius.circular(24)),
          border: Border(top: BorderSide(color: AppColors.glassBorderLight)),
        ),
        padding: const EdgeInsets.fromLTRB(20, 16, 20, 20),
        child: SafeArea(
          top: false,
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Center(
                child: Container(
                  width: 36,
                  height: 4,
                  decoration: BoxDecoration(
                    color: AppColors.glassBorderLight,
                    borderRadius: BorderRadius.circular(2),
                  ),
                ),
              ),
              const SizedBox(height: 18),
              const Text(
                "What's on your mind?",
                style: TextStyle(
                  color: AppColors.textPrimary,
                  fontSize: 18,
                  fontWeight: FontWeight.w700,
                  letterSpacing: -0.3,
                ),
              ),
              const SizedBox(height: 16),
              Wrap(
                spacing: 8,
                runSpacing: 8,
                children: [
                  for (final category in _feedbackCategories)
                    _CategoryChip(
                      label: category.label,
                      selected: _selectedCategory == category.value,
                      onTap: () =>
                          setState(() => _selectedCategory = category.value),
                    ),
                ],
              ),
              const SizedBox(height: 16),
              FauxGlassCard.section(
                child: TextField(
                  controller: _controller,
                  enabled: !_isSubmitting,
                  maxLines: 5,
                  maxLength: 1000,
                  style: const TextStyle(
                    color: AppColors.textPrimary,
                    fontSize: 15,
                    height: 1.4,
                  ),
                  cursorColor: AppColors.accent,
                  decoration: const InputDecoration(
                    hintText:
                        'Tell us anything, what you love, what feels off, '
                        'what you wish it did.',
                    hintStyle: TextStyle(
                      color: AppColors.textTertiary,
                      fontSize: 14,
                    ),
                    filled: false,
                    border: InputBorder.none,
                    enabledBorder: InputBorder.none,
                    focusedBorder: InputBorder.none,
                    disabledBorder: InputBorder.none,
                    isCollapsed: true,
                    counterText: '',
                  ),
                ),
              ),
              if (_errorMessage != null) ...[
                const SizedBox(height: 12),
                Text(
                  _errorMessage!,
                  style: const TextStyle(color: AppColors.error, fontSize: 13),
                ),
              ],
              const SizedBox(height: 16),
              _FeedbackSubmitButton(
                enabled: _hasText && !_isSubmitting,
                isSubmitting: _isSubmitting,
                onTap: _submit,
              ),
            ],
          ),
        ),
      ),
    );
  }
}

class _CategoryChip extends StatelessWidget {
  final String label;
  final bool selected;
  final VoidCallback onTap;

  const _CategoryChip({
    required this.label,
    required this.selected,
    required this.onTap,
  });

  @override
  Widget build(BuildContext context) {
    if (!selected) {
      return GestureDetector(
        onTap: onTap,
        child: FauxGlassCard.pill(
          child: Text(
            label,
            style: const TextStyle(
              color: AppColors.textSecondary,
              fontSize: 14,
            ),
          ),
        ),
      );
    }
    // Selected state uses a dynamic accent border/gradient, so the default
    // constructor is used here rather than the pill preset.
    return GestureDetector(
      onTap: onTap,
      child: FauxGlassCard(
        borderRadius: 20,
        padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
        borderColor: AppColors.accent.withValues(alpha: 0.5),
        gradient: LinearGradient(
          colors: [
            AppColors.accent.withValues(alpha: 0.18),
            AppColors.accent.withValues(alpha: 0.08),
          ],
        ),
        child: Text(
          label,
          style: const TextStyle(
            color: AppColors.accent,
            fontSize: 14,
            fontWeight: FontWeight.w600,
          ),
        ),
      ),
    );
  }
}

class _FeedbackSubmitButton extends StatelessWidget {
  final bool enabled;
  final bool isSubmitting;
  final VoidCallback onTap;

  const _FeedbackSubmitButton({
    required this.enabled,
    required this.isSubmitting,
    required this.onTap,
  });

  @override
  Widget build(BuildContext context) {
    return GestureDetector(
      onTap: enabled ? onTap : null,
      child: Opacity(
        opacity: enabled || isSubmitting ? 1.0 : 0.4,
        child: FauxGlassCard(
          borderRadius: 16,
          padding: const EdgeInsets.symmetric(vertical: 15),
          borderColor: AppColors.accent.withValues(alpha: 0.35),
          gradient: LinearGradient(
            colors: [
              AppColors.accent.withValues(alpha: 0.22),
              AppColors.accent.withValues(alpha: 0.10),
            ],
          ),
          child: Center(
            child: isSubmitting
                ? const SizedBox(
                    width: 18,
                    height: 18,
                    child: CircularProgressIndicator(
                      strokeWidth: 2,
                      color: AppColors.accent,
                    ),
                  )
                : const Text(
                    'Send',
                    style: TextStyle(
                      color: AppColors.textPrimary,
                      fontSize: 16,
                      fontWeight: FontWeight.w600,
                    ),
                  ),
          ),
        ),
      ),
    );
  }
}
