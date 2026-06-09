import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';
import 'package:provider/provider.dart';
import 'package:url_launcher/url_launcher.dart';

import '../../../core/theme/app_colors.dart';
import '../../../core/theme/glass_card.dart';
import '../../viewmodels/auth_viewmodel.dart';
import '../../viewmodels/settings_viewmodel.dart';
import '../../widgets/error_display.dart';
import '../../widgets/loading_indicator.dart';
import '../reminders/reminders_screen.dart';
import 'aura_profile_screen.dart';

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
    if (!mounted) return;
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
          style: TextStyle(color: AppColors.textPrimary, fontWeight: FontWeight.w700),
        ),
        content: const Text(
          'All your data — chats, reminders, and your Aura profile — will be permanently deleted. This cannot be undone.',
          style: TextStyle(color: AppColors.textSecondary, height: 1.5),
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(ctx, false),
            child: const Text('Cancel', style: TextStyle(color: AppColors.textTertiary)),
          ),
          TextButton(
            onPressed: () => Navigator.pop(ctx, true),
            child: const Text(
              'Delete forever',
              style: TextStyle(color: AppColors.error, fontWeight: FontWeight.w600),
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
        SnackBar(
          content: Text(errorMessage),
          backgroundColor: AppColors.error,
        ),
      );
    }
  }

  Future<void> _showFeedbackSheet(BuildContext context) async {
    final settingsVm = context.read<SettingsViewModel>();
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
    if (sent == true && mounted) {
      messenger.showSnackBar(
        const SnackBar(
          content: Text(
            'Got it — thanks for the feedback.',
            style: TextStyle(color: AppColors.textPrimary),
          ),
          backgroundColor: AppColors.surfaceVariant,
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
                    horizontal: 20, vertical: 12),
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
                          title: 'Wake Word',
                          subtitle: 'Activate with "Hey Buddy"',
                          value: settings?.wakeWordEnabled ?? false,
                          onChanged: settingsVm.toggleWakeWord,
                        ),
                        const SizedBox(height: 8),
                        _GlassToggleTile(
                          title: 'Voice Responses',
                          subtitle: 'Read responses aloud (TTS)',
                          value: settings?.ttsEnabled ?? true,
                          onChanged: settingsVm.toggleTts,
                        ),

                        // ── Reminders ────────────────────────────────────────
                        _SectionLabel('Reminders'),
                        _GlassNavTile(
                          icon: Icons.notifications_outlined,
                          title: 'View Reminders',
                          subtitle: 'See all scheduled reminders',
                          onTap: () => Navigator.push(
                            context,
                            RemindersScreen.route(),
                          ),
                        ),

                        // Aura profile
                        _SectionLabel('Aura Memory'),
                        _GlassNavTile(
                          icon: Icons.auto_awesome_outlined,
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
                          icon: Icons.star_outline_rounded,
                          title: 'Upgrade Plan',
                          subtitle: 'View plans and manage subscription',
                          onTap: () => context.push('/paywall'),
                        ),

                        // Account
                        _SectionLabel('Account'),
                        if (user != null) ...[
                          _GlassInfoTile(
                              label: 'Name', value: user.displayName),
                          const SizedBox(height: 8),
                          _GlassInfoTile(
                              label: 'Email', value: user.email),
                        ],

                        // ── Feedback ─────────────────────────────────────────
                        _SectionLabel('Feedback'),
                        _GlassNavTile(
                          icon: Icons.feedback_outlined,
                          title: 'Send Feedback',
                          subtitle: 'Tell us what to change or fix',
                          onTap: () => _showFeedbackSheet(context),
                        ),

                        // ── Legal ────────────────────────────────────────────
                        _SectionLabel('Legal'),
                        _GlassNavTile(
                          icon: Icons.privacy_tip_outlined,
                          title: 'Privacy Policy',
                          subtitle: 'How we handle your data',
                          onTap: () => launchUrl(
                            Uri.parse('https://varuntej.dev/aura/privacy-policy'),
                            mode: LaunchMode.externalApplication,
                          ),
                        ),
                        const SizedBox(height: 8),
                        _GlassNavTile(
                          icon: Icons.description_outlined,
                          title: 'Terms of Service',
                          subtitle: 'Terms and conditions',
                          onTap: () => launchUrl(
                            Uri.parse('https://varuntej.dev/aura/terms-of-service'),
                            mode: LaunchMode.externalApplication,
                          ),
                        ),

                        const SizedBox(height: 28),
                        _GlassSignOutButton(
                          onTap: () => _signOut(context),
                        ),
                        const SizedBox(height: 12),
                        _GlassDeleteAccountButton(
                          onTap: () => _confirmDeleteAccount(context),
                        ),

                        const SizedBox(height: 28),
                        Center(
                          child: Text(
                            'Aura v1.0.0',
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

// Delete account button

class _GlassDeleteAccountButton extends StatelessWidget {
  final VoidCallback onTap;
  const _GlassDeleteAccountButton({required this.onTap});

  @override
  Widget build(BuildContext context) {
    return GestureDetector(
      onTap: onTap,
      child: FauxGlassCard(
        borderRadius: 16,
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
  final String title;
  final String subtitle;
  final bool value;
  final ValueChanged<bool> onChanged;

  const _GlassToggleTile({
    required this.title,
    required this.subtitle,
    required this.value,
    required this.onChanged,
  });

  @override
  Widget build(BuildContext context) {
    return FauxGlassCard.toggleTile(
      child: SwitchListTile(
        title: Text(
          title,
          style: const TextStyle(
              color: AppColors.textPrimary, fontSize: 15),
        ),
        subtitle: Text(
          subtitle,
          style: const TextStyle(
              color: AppColors.textTertiary, fontSize: 13),
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
            Container(
              width: 36,
              height: 36,
              decoration: BoxDecoration(
                color: AppColors.accent.withValues(alpha: 0.12),
                borderRadius: BorderRadius.circular(10),
              ),
              child: Icon(icon, size: 18, color: AppColors.accent),
            ),
            const SizedBox(width: 12),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    title,
                    style: const TextStyle(
                        color: AppColors.textPrimary, fontSize: 15),
                  ),
                  const SizedBox(height: 2),
                  Text(
                    subtitle,
                    style: const TextStyle(
                        color: AppColors.textTertiary, fontSize: 13),
                  ),
                ],
              ),
            ),
            const Icon(Icons.chevron_right,
                size: 18, color: AppColors.textTertiary),
          ],
        ),
      ),
    );
  }
}

// Info tile

class _GlassInfoTile extends StatelessWidget {
  final String label;
  final String value;

  const _GlassInfoTile({required this.label, required this.value});

  @override
  Widget build(BuildContext context) {
    return FauxGlassCard.navTile(
      child: Row(
        children: [
          Text(
            label,
            style: const TextStyle(
                color: AppColors.textTertiary, fontSize: 14),
          ),
          const Spacer(),
          Text(
            value,
            style: const TextStyle(
                color: AppColors.textPrimary, fontSize: 14),
          ),
        ],
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
    final error =
        await widget.onSubmit(_controller.text, _selectedCategory);
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
      padding: EdgeInsets.only(bottom: MediaQuery.of(context).viewInsets.bottom),
      child: Container(
        decoration: const BoxDecoration(
          color: AppColors.deepBackground,
          borderRadius: BorderRadius.vertical(top: Radius.circular(24)),
          border: Border(
            top: BorderSide(color: AppColors.glassBorderLight),
          ),
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
                      color: AppColors.textPrimary, fontSize: 15, height: 1.4),
                  cursorColor: AppColors.accent,
                  decoration: const InputDecoration(
                    hintText: 'Tell us anything — what you love, what feels off, '
                        'what you wish it did.',
                    hintStyle:
                        TextStyle(color: AppColors.textTertiary, fontSize: 14),
                    border: InputBorder.none,
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
                color: AppColors.textSecondary, fontSize: 14),
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
