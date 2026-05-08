import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';
import 'package:provider/provider.dart';

import '../../../core/theme/app_colors.dart';
import '../../../core/theme/glass_card.dart';
import '../../viewmodels/auth_viewmodel.dart';
import '../../viewmodels/settings_viewmodel.dart';
import '../../widgets/error_display.dart';
import '../../widgets/loading_indicator.dart';
import '../reminders/reminders_screen.dart';

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
                        _GlassReminderLeadTile(
                          minutes:
                              settings?.defaultReminderLeadMinutes ?? 10,
                          onChanged: settingsVm.setReminderLeadMinutes,
                        ),
                        const SizedBox(height: 8),
                        _GlassNavTile(
                          icon: Icons.notifications_outlined,
                          title: 'View Reminders',
                          subtitle: 'See all scheduled reminders',
                          onTap: () => Navigator.push(
                            context,
                            RemindersScreen.route(context),
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
                        const SizedBox(height: 20),
                        _GlassSignOutButton(
                          onTap: () =>
                              context.read<AuthViewModel>().signOut(),
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
    return FauxGlassCard(
      borderRadius: 16,
      padding: const EdgeInsets.symmetric(horizontal: 4),
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

//  Reminder lead tile

class _GlassReminderLeadTile extends StatelessWidget {
  final int minutes;
  final ValueChanged<int> onChanged;

  const _GlassReminderLeadTile(
      {required this.minutes, required this.onChanged});

  @override
  Widget build(BuildContext context) {
    return FauxGlassCard(
      borderRadius: 16,
      padding: const EdgeInsets.fromLTRB(16, 14, 16, 8),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            mainAxisAlignment: MainAxisAlignment.spaceBetween,
            children: [
              const Text(
                'Default reminder lead time',
                style: TextStyle(
                    color: AppColors.textPrimary, fontSize: 15),
              ),
              Text(
                '$minutes min',
                style: const TextStyle(
                  color: AppColors.accent,
                  fontSize: 14,
                  fontWeight: FontWeight.w600,
                ),
              ),
            ],
          ),
          SliderTheme(
            data: SliderTheme.of(context).copyWith(
              activeTrackColor: AppColors.accent,
              inactiveTrackColor: AppColors.glassBorderDim,
              thumbColor: AppColors.accent,
              overlayColor: AppColors.accentGlow,
            ),
            child: Slider(
              value: minutes.toDouble(),
              min: 1,
              max: 60,
              divisions: 59,
              onChanged: (v) => onChanged(v.round()),
            ),
          ),
        ],
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
      child: FauxGlassCard(
        borderRadius: 16,
        padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 14),
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
    return FauxGlassCard(
      borderRadius: 16,
      padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 14),
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

//  Sign-out button 

class _GlassSignOutButton extends StatelessWidget {
  final VoidCallback onTap;
  const _GlassSignOutButton({required this.onTap});

  @override
  Widget build(BuildContext context) {
    return GestureDetector(
      onTap: onTap,
      child: FauxGlassCard(
        borderRadius: 16,
        padding: const EdgeInsets.symmetric(vertical: 16),
        borderColor: AppColors.error.withValues(alpha: 0.3),
        gradient: LinearGradient(
          begin: Alignment.topLeft,
          end: Alignment.bottomRight,
          colors: [
            AppColors.error.withValues(alpha: 0.10),
            AppColors.error.withValues(alpha: 0.04),
          ],
        ),
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
