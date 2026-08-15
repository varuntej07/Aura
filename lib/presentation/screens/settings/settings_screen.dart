import 'dart:io' show Platform;

import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';
import 'package:package_info_plus/package_info_plus.dart';
import 'package:provider/provider.dart';
import 'package:url_launcher/url_launcher.dart';

import '../../../core/constants/alarm_tones.dart';
import '../../../core/constants/buddy_voices.dart';
import '../../../core/errors/app_exception.dart';
import '../../../core/theme/app_colors.dart';
import '../../../core/theme/glass_card.dart';
import '../../../data/models/user_model.dart';
import '../../../data/services/voice_launcher_bridge.dart';
import '../../viewmodels/auth_viewmodel.dart';
import '../../viewmodels/settings_viewmodel.dart';
import '../../widgets/error_display.dart';
import '../../widgets/pressable_tile.dart';

class SettingsScreen extends StatefulWidget {
  const SettingsScreen({super.key});

  @override
  State<SettingsScreen> createState() => _SettingsScreenState();
}

class _SettingsScreenState extends State<SettingsScreen> {
  Future<PackageInfo>? _packageInfo;

  @override
  void initState() {
    super.initState();
    // Seeded synchronously, not in a post-frame callback: the VM's loadUser is
    // silent, so the first build already reads the real settings. Deferring it
    // by a frame is what made the page paint defaults and then visibly correct
    // itself while the route was still sliding in.
    final user = context.read<AuthViewModel>().user;
    if (user != null) {
      context.read<SettingsViewModel>().loadUser(user);
    }
  }

  Future<void> _signOut(BuildContext context) async {
    final authVm = context.read<AuthViewModel>();
    await authVm.signOut();
    if (!context.mounted) return;
    // Settings was pushed via Navigator (not GoRouter), so the redirect won't
    // clear this screen on its own — navigating to the sign-in screen explicitly.
    context.go('/login');
  }

  Future<void> _showAccountActions(BuildContext context) async {
    await showModalBottomSheet<void>(
      context: context,
      backgroundColor: Colors.transparent,
      builder: (sheetContext) => _AccountActionsSheet(
        onSignOut: () {
          Navigator.pop(sheetContext);
          _signOut(context);
        },
        onDeleteAccount: () {
          Navigator.pop(sheetContext);
          _confirmDeleteAccount(context);
        },
      ),
    );
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
              //
              // No page-wide Consumer. This list used to sit inside a
              // Consumer2<SettingsViewModel, AuthViewModel>, so a notify from
              // either — and AuthViewModel notifies off a Firestore user stream
              // and an entitlement stream — rebuilt all ~25 gradient tiles. The
              // handful of rows that actually depend on state subscribe for
              // themselves below; everything else is const and never rebuilds.
              Expanded(
                child: ListView(
                  padding: const EdgeInsets.fromLTRB(16, 4, 16, 32),
                  children: [
                    Selector<SettingsViewModel, AppException?>(
                      selector: (_, vm) => vm.error,
                      builder: (context, error, _) => error == null
                          ? const SizedBox.shrink()
                          : Padding(
                              padding: const EdgeInsets.only(bottom: 12),
                              child: ErrorDisplay(
                                error: error,
                                onDismiss: context
                                    .read<SettingsViewModel>()
                                    .clearError,
                              ),
                            ),
                    ),

                    // ── Voice ───────────────────────────────────────────
                    const _SectionLabel('Voice'),
                    Selector<SettingsViewModel, String>(
                      selector: (_, vm) => buddyVoiceFor(
                        vm.settings?.ttsVoiceId.isEmpty ?? true
                            ? kDefaultBuddyVoiceSlug
                            : vm.settings!.ttsVoiceId,
                      ).label,
                      builder: (context, voiceLabel, _) => _GlassNavTile(
                        icon: _SettingsIconKind.waveform,
                        iconColor: _SettingsIconColors.purple,
                        title: "Buddy's voice",
                        subtitle: voiceLabel,
                        onTap: () => context.push('/settings/voice'),
                      ),
                    ),
                    const SizedBox(height: 8),
                    Selector<SettingsViewModel, bool>(
                      selector: (_, vm) => vm.settings?.ttsEnabled ?? true,
                      builder: (context, enabled, _) => _GlassToggleTile(
                        icon: _SettingsIconKind.volume,
                        iconColor: _SettingsIconColors.green,
                        title: 'Voice Responses',
                        subtitle: 'Read responses aloud (TTS)',
                        value: enabled,
                        onChanged: context.read<SettingsViewModel>().toggleTts,
                      ),
                    ),
                    // Home-screen widget: one tap opens the app with mic on.
                    // Android-only (iOS WidgetKit ships separately).
                    if (Platform.isAndroid) ...[
                      const SizedBox(height: 8),
                      _GlassNavTile(
                        icon: _SettingsIconKind.phone,
                        iconColor: _SettingsIconColors.blue,
                        title: 'Add to home screen',
                        subtitle: 'One tap widget that opens Buddy',
                        onTap: () => _addVoiceWidget(context),
                      ),
                    ],

                    // ── Buddy on your PC ─────────────────────────────────
                    const _SectionLabel('Buddy on your PC'),
                    _GlassNavTile(
                      icon: _SettingsIconKind.desktop,
                      iconColor: _SettingsIconColors.blue,
                      title: 'Link this PC',
                      subtitle: 'Get a code to sign in Buddy on your desktop',
                      onTap: () => context.push('/settings/link-device'),
                    ),

                    // ── Reminders ────────────────────────────────────────
                    const _SectionLabel('Reminders'),
                    _GlassNavTile(
                      icon: _SettingsIconKind.bell,
                      iconColor: _SettingsIconColors.coral,
                      title: 'View Reminders',
                      subtitle: 'See all scheduled reminders',
                      onTap: () => context.push('/reminders'),
                    ),
                    const SizedBox(height: 8),
                    Selector<SettingsViewModel, String>(
                      selector: (_, vm) => vm.settings?.alarmTone ?? '',
                      builder: (context, storedTone, _) {
                        final slug = displayAlarmToneSlug(storedTone);
                        final label = switch (slug) {
                          kAlarmToneDevice => 'From your device',
                          kAlarmToneSystemDefault => 'Phone default',
                          _ => alarmToneFor(slug)?.label ?? 'Phone default',
                        };
                        return _GlassNavTile(
                          icon: _SettingsIconKind.volume,
                          iconColor: _SettingsIconColors.green,
                          title: 'Alarm sound',
                          subtitle: label,
                          onTap: () => context.push('/settings/alarm-sound'),
                        );
                      },
                    ),

                    const _SectionLabel('Connectors'),
                    _GlassNavTile(
                      icon: _SettingsIconKind.link,
                      iconColor: _SettingsIconColors.green,
                      title: 'Connectors',
                      subtitle: 'Calendar, Gmail & more',
                      onTap: () => context.push('/settings/connectors'),
                    ),

                    // Aura memory — consent toggle + profile
                    const _SectionLabel('Aura'),
                    _GlassNavTile(
                      icon: _SettingsIconKind.brain,
                      iconColor: _SettingsIconColors.purple,
                      title: 'Your Aura',
                      subtitle: 'What Buddy remembers about you',
                      onTap: () => context.push('/settings/aura-profile'),
                    ),

                    // Subscription
                    const _SectionLabel('Subscription'),
                    _GlassNavTile(
                      icon: _SettingsIconKind.premium,
                      iconColor: _SettingsIconColors.amber,
                      title: 'Upgrade Plan',
                      subtitle: 'View plans and manage subscription',
                      onTap: () => context.push('/paywall'),
                    ),

                    // Account
                    const _SectionLabel('Account'),
                    Selector<AuthViewModel, UserModel?>(
                      selector: (_, vm) => vm.user,
                      builder: (context, user, _) => user == null
                          ? _GlassSignInButton(
                              onTap: () => context.go('/login'),
                            )
                          : Column(
                              children: [
                                _GlassInfoTile(
                                  icon: _SettingsIconKind.person,
                                  iconColor: _SettingsIconColors.blue,
                                  label: 'Name',
                                  value: user.displayName,
                                ),
                                const SizedBox(height: 8),
                                _GlassInfoTile(
                                  icon: _SettingsIconKind.email,
                                  iconColor: _SettingsIconColors.purple,
                                  label: 'Email',
                                  value: user.email,
                                ),
                                const SizedBox(height: 8),
                                _GlassNavTile(
                                  icon: _SettingsIconKind.person,
                                  iconColor: _SettingsIconColors.blue,
                                  title: 'Manage account',
                                  subtitle: 'Sign out or delete your account',
                                  onTap: () => _showAccountActions(context),
                                ),
                              ],
                            ),
                    ),

                    // ── Feedback ─────────────────────────────────────────
                    const _SectionLabel('Feedback'),
                    _GlassNavTile(
                      icon: _SettingsIconKind.feedback,
                      iconColor: _SettingsIconColors.coral,
                      title: 'Send Feedback',
                      subtitle: 'Tell us what to change or fix',
                      onTap: () => showFeedbackSheet(context),
                    ),

                    // ── Legal ────────────────────────────────────────────
                    const _SectionLabel('Legal'),
                    _GlassNavTile(
                      icon: _SettingsIconKind.privacy,
                      iconColor: _SettingsIconColors.blue,
                      title: 'Privacy Policy',
                      subtitle: 'How we handle your data',
                      onTap: () => launchUrl(
                        Uri.parse('https://auravoiceapp.com/privacy-policy'),
                        mode: LaunchMode.externalApplication,
                      ),
                    ),
                    const SizedBox(height: 8),
                    _GlassNavTile(
                      icon: _SettingsIconKind.document,
                      iconColor: _SettingsIconColors.purple,
                      title: 'Terms of Service',
                      subtitle: 'Terms and conditions',
                      onTap: () => launchUrl(
                        Uri.parse('https://auravoiceapp.com/terms-of-service'),
                        mode: LaunchMode.externalApplication,
                      ),
                    ),

                    const SizedBox(height: 28),
                    Center(
                      child: FutureBuilder<PackageInfo>(
                        future: _packageInfo ??= PackageInfo.fromPlatform(),
                        builder: (context, snapshot) => Text(
                          snapshot.hasData
                              ? 'Aura v${snapshot.data!.version}'
                              : 'Aura',
                          style: const TextStyle(
                            color: AppColors.textTertiary,
                            fontSize: 12,
                          ),
                        ),
                      ),
                    ),
                  ],
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

const double _settingsTileHeight = 68;

class _GlassToggleTile extends StatelessWidget {
  final _SettingsIconKind icon;
  final Color iconColor;
  final String title;
  final String subtitle;
  final bool value;
  final ValueChanged<bool> onChanged;

  const _GlassToggleTile({
    required this.icon,
    required this.iconColor,
    required this.title,
    required this.subtitle,
    required this.value,
    required this.onChanged,
  });

  @override
  Widget build(BuildContext context) {
    return SizedBox(
      height: _settingsTileHeight,
      child: FauxGlassCard.toggleTile(
        child: SwitchListTile(
          dense: true,
          secondary: _SettingsIcon(icon, color: iconColor),
          title: Text(
            title,
            style: const TextStyle(
              color: AppColors.textPrimary,
              fontSize: 15,
              height: 1.2,
            ),
          ),
          subtitle: Text(
            subtitle,
            maxLines: 1,
            overflow: TextOverflow.ellipsis,
            style: const TextStyle(
              color: AppColors.textTertiary,
              fontSize: 13,
              height: 1.2,
            ),
          ),
          value: value,
          onChanged: onChanged,
          activeThumbColor: AppColors.accent,
          activeTrackColor: AppColors.accent.withValues(alpha: 0.3),
        ),
      ),
    );
  }
}

// Nav tile

class _GlassNavTile extends StatelessWidget {
  final _SettingsIconKind icon;
  final Color iconColor;
  final String title;
  final String subtitle;
  final VoidCallback onTap;

  const _GlassNavTile({
    required this.icon,
    required this.iconColor,
    required this.title,
    required this.subtitle,
    required this.onTap,
  });

  @override
  Widget build(BuildContext context) {
    return SizedBox(
      height: _settingsTileHeight,
      child: PressableTile(
        onTap: onTap,
        child: FauxGlassCard.navTile(
          child: Row(
            children: [
              _SettingsIcon(icon, color: iconColor),
              const SizedBox(width: 12),
              Expanded(
                child: Column(
                  mainAxisAlignment: MainAxisAlignment.center,
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      title,
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      style: const TextStyle(
                        color: AppColors.textPrimary,
                        fontSize: 15,
                        height: 1.2,
                      ),
                    ),
                    const SizedBox(height: 2),
                    Text(
                      subtitle,
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      style: const TextStyle(
                        color: AppColors.textTertiary,
                        fontSize: 13,
                        height: 1.2,
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
      ),
    );
  }
}

enum _SettingsIconKind {
  waveform,
  volume,
  phone,
  desktop,
  bell,
  link,
  brain,
  premium,
  person,
  email,
  feedback,
  privacy,
  document,
  signOut,
  delete,
}

class _SettingsIcon extends StatelessWidget {
  final _SettingsIconKind icon;
  final Color color;

  const _SettingsIcon(this.icon, {required this.color});

  @override
  Widget build(BuildContext context) {
    return SizedBox(
      width: 32,
      height: 32,
      child: CustomPaint(
        painter: _SettingsIconPainter(icon: icon, color: color),
      ),
    );
  }
}

class _SettingsIconPainter extends CustomPainter {
  final _SettingsIconKind icon;
  final Color color;

  const _SettingsIconPainter({required this.icon, required this.color});

  @override
  void paint(Canvas canvas, Size size) {
    canvas.save();
    canvas.scale(size.width / 24, size.height / 24);
    final paint = Paint()
      ..color = color
      ..style = PaintingStyle.stroke
      ..strokeWidth = 2
      ..strokeCap = StrokeCap.round
      ..strokeJoin = StrokeJoin.round;

    void line(double x1, double y1, double x2, double y2) {
      canvas.drawLine(Offset(x1, y1), Offset(x2, y2), paint);
    }

    switch (icon) {
      case _SettingsIconKind.waveform:
        for (final bar in const [
          (3.0, 10.0, 14.0),
          (6.5, 7.5, 16.5),
          (10.0, 4.5, 19.5),
          (14.0, 6.0, 18.0),
          (17.5, 8.5, 15.5),
          (21.0, 10.5, 13.5),
        ]) {
          line(bar.$1, bar.$2, bar.$1, bar.$3);
        }
        break;
      case _SettingsIconKind.volume:
        canvas.drawPath(
          Path()
            ..moveTo(4, 10)
            ..lineTo(8, 10)
            ..lineTo(13, 6)
            ..lineTo(13, 18)
            ..lineTo(8, 14)
            ..lineTo(4, 14)
            ..close(),
          paint,
        );
        canvas.drawArc(
          const Rect.fromLTRB(12, 8, 20, 16),
          -1.05,
          2.1,
          false,
          paint,
        );
        canvas.drawArc(
          const Rect.fromLTRB(11, 5, 23, 19),
          -0.85,
          1.7,
          false,
          paint,
        );
        break;
      case _SettingsIconKind.phone:
        canvas.drawRRect(
          RRect.fromRectAndRadius(
            const Rect.fromLTRB(6, 3, 18, 21),
            const Radius.circular(2.5),
          ),
          paint,
        );
        line(10, 6, 14, 6);
        canvas.drawCircle(const Offset(12, 17.5), 0.8, paint);
        break;
      case _SettingsIconKind.desktop:
        canvas.drawRRect(
          RRect.fromRectAndRadius(
            const Rect.fromLTRB(3, 4, 21, 16),
            const Radius.circular(2),
          ),
          paint,
        );
        line(9, 20, 15, 20);
        line(12, 16, 12, 20);
        break;
      case _SettingsIconKind.bell:
        canvas.drawPath(
          Path()
            ..moveTo(5, 16)
            ..quadraticBezierTo(7, 14, 7, 10)
            ..quadraticBezierTo(7, 5, 12, 5)
            ..quadraticBezierTo(17, 5, 17, 10)
            ..quadraticBezierTo(17, 14, 19, 16)
            ..lineTo(5, 16),
          paint,
        );
        line(4, 18, 20, 18);
        canvas.drawArc(
          const Rect.fromLTRB(9, 17, 15, 22),
          0,
          3.14,
          false,
          paint,
        );
        break;
      case _SettingsIconKind.link:
        canvas.drawPath(
          Path()
            ..moveTo(10, 8)
            ..lineTo(8.5, 6.5)
            ..cubicTo(5.5, 3.5, 1, 8, 4, 11)
            ..lineTo(6, 13)
            ..cubicTo(7.8, 14.8, 10.2, 14.8, 12, 13),
          paint,
        );
        canvas.drawPath(
          Path()
            ..moveTo(14, 16)
            ..lineTo(15.5, 17.5)
            ..cubicTo(18.5, 20.5, 23, 16, 20, 13)
            ..lineTo(18, 11)
            ..cubicTo(16.2, 9.2, 13.8, 9.2, 12, 11),
          paint,
        );
        line(8.5, 15.5, 15.5, 8.5);
        break;
      case _SettingsIconKind.brain:
        line(12, 5, 12, 18);
        canvas.drawPath(
          Path()
            ..moveTo(12, 5)
            ..cubicTo(10.8, 2.5, 7.2, 2.4, 6, 5.1)
            ..cubicTo(3.2, 5.6, 2.2, 8.5, 3.5, 10.9)
            ..cubicTo(1.7, 13.1, 2.2, 16.2, 4, 17.5)
            ..cubicTo(3.8, 20.1, 7.1, 22.2, 9.4, 20.7)
            ..cubicTo(10.5, 20, 11.2, 18.8, 12, 18)
            ..moveTo(12, 5)
            ..cubicTo(13.2, 2.5, 16.8, 2.4, 18, 5.1)
            ..cubicTo(20.8, 5.6, 21.8, 8.5, 20.5, 10.9)
            ..cubicTo(22.3, 13.1, 21.8, 16.2, 20, 17.5)
            ..cubicTo(20.2, 20.1, 16.9, 22.2, 14.6, 20.7)
            ..cubicTo(13.5, 20, 12.8, 18.8, 12, 18),
          paint,
        );
        canvas.drawPath(
          Path()
            ..moveTo(9, 13)
            ..cubicTo(10.8, 12.4, 12, 10.8, 12, 9)
            ..cubicTo(12, 10.8, 13.2, 12.4, 15, 13),
          paint,
        );
        canvas.drawArc(
          const Rect.fromLTRB(4.5, 4.2, 8.5, 8.5),
          -0.2,
          1.35,
          false,
          paint,
        );
        canvas.drawArc(
          const Rect.fromLTRB(15.5, 4.2, 19.5, 8.5),
          2,
          1.35,
          false,
          paint,
        );
        break;
      case _SettingsIconKind.premium:
        canvas.drawPath(
          Path()
            ..moveTo(4, 8)
            ..lineTo(8, 12)
            ..lineTo(12, 6)
            ..lineTo(16, 12)
            ..lineTo(20, 8)
            ..lineTo(18, 18)
            ..lineTo(6, 18)
            ..close(),
          paint,
        );
        line(7, 21, 17, 21);
        break;
      case _SettingsIconKind.person:
        canvas.drawCircle(const Offset(12, 8), 4, paint);
        canvas.drawPath(
          Path()
            ..moveTo(4, 21)
            ..cubicTo(5, 15, 19, 15, 20, 21),
          paint,
        );
        break;
      case _SettingsIconKind.email:
        canvas.drawCircle(const Offset(12, 12), 8, paint);
        canvas.drawCircle(const Offset(12, 12), 3, paint);
        canvas.drawPath(
          Path()
            ..moveTo(15, 9)
            ..lineTo(15, 14)
            ..cubicTo(15, 17, 20, 16, 20, 12),
          paint,
        );
        break;
      case _SettingsIconKind.feedback:
        canvas.drawRRect(
          RRect.fromRectAndRadius(
            const Rect.fromLTRB(3, 4, 21, 17),
            const Radius.circular(3),
          ),
          paint,
        );
        canvas.drawPath(
          Path()
            ..moveTo(8, 17)
            ..lineTo(8, 21)
            ..lineTo(13, 17),
          paint,
        );
        line(7, 9, 17, 9);
        line(7, 13, 14, 13);
        break;
      case _SettingsIconKind.privacy:
        canvas.drawPath(
          Path()
            ..moveTo(12, 3)
            ..lineTo(20, 6)
            ..lineTo(19, 13)
            ..cubicTo(18, 18, 14, 20, 12, 21)
            ..cubicTo(10, 20, 6, 18, 5, 13)
            ..lineTo(4, 6)
            ..close(),
          paint,
        );
        line(8.5, 12, 11, 14.5);
        line(11, 14.5, 16, 9.5);
        break;
      case _SettingsIconKind.document:
        canvas.drawPath(
          Path()
            ..moveTo(6, 3)
            ..lineTo(15, 3)
            ..lineTo(19, 7)
            ..lineTo(19, 21)
            ..lineTo(6, 21)
            ..close()
            ..moveTo(15, 3)
            ..lineTo(15, 7)
            ..lineTo(19, 7),
          paint,
        );
        line(9, 12, 16, 12);
        line(9, 16, 16, 16);
        break;
      case _SettingsIconKind.signOut:
        canvas.drawPath(
          Path()
            ..moveTo(10, 4)
            ..lineTo(5, 4)
            ..lineTo(5, 20)
            ..lineTo(10, 20),
          paint,
        );
        line(10, 12, 21, 12);
        line(17, 8, 21, 12);
        line(17, 16, 21, 12);
        break;
      case _SettingsIconKind.delete:
        canvas.drawRRect(
          RRect.fromRectAndRadius(
            const Rect.fromLTRB(6, 7, 18, 21),
            const Radius.circular(2),
          ),
          paint,
        );
        line(4, 7, 20, 7);
        line(9, 3, 15, 3);
        line(10, 11, 10, 17);
        line(14, 11, 14, 17);
        break;
    }
    canvas.restore();
  }

  @override
  bool shouldRepaint(covariant _SettingsIconPainter oldDelegate) {
    return oldDelegate.icon != icon || oldDelegate.color != color;
  }
}

class _SettingsIconColors {
  static const purple = Color(0xFF7659D8);
  static const amber = Color(0xFFD98A22);
  static const green = Color(0xFF50A950);
  static const blue = Color(0xFF5A7DE0);
  static const coral = Color(0xFFE47160);
}

// Info tile

class _GlassInfoTile extends StatelessWidget {
  final _SettingsIconKind icon;
  final Color iconColor;
  final String label;
  final String value;

  const _GlassInfoTile({
    required this.icon,
    required this.iconColor,
    required this.label,
    required this.value,
  });

  @override
  Widget build(BuildContext context) {
    return SizedBox(
      height: _settingsTileHeight,
      child: FauxGlassCard.navTile(
        child: Row(
          children: [
            _SettingsIcon(icon, color: iconColor),
            const SizedBox(width: 12),
            Text(
              label,
              style: const TextStyle(
                color: AppColors.textTertiary,
                fontSize: 14,
              ),
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
      child: SizedBox(
        height: _settingsTileHeight,
        child: FauxGlassCard(
          borderRadius: 16,
          padding: EdgeInsets.zero,
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
      ),
    );
  }
}

class _AccountActionsSheet extends StatelessWidget {
  final VoidCallback onSignOut;
  final VoidCallback onDeleteAccount;

  const _AccountActionsSheet({
    required this.onSignOut,
    required this.onDeleteAccount,
  });

  @override
  Widget build(BuildContext context) {
    return Container(
      decoration: const BoxDecoration(
        color: AppColors.deepBackground,
        borderRadius: BorderRadius.vertical(top: Radius.circular(24)),
        border: Border(top: BorderSide(color: AppColors.glassBorderLight)),
      ),
      padding: const EdgeInsets.fromLTRB(20, 12, 20, 20),
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
            const SizedBox(height: 14),
            const Text(
              'Manage account',
              style: TextStyle(
                color: AppColors.textPrimary,
                fontSize: 18,
                fontWeight: FontWeight.w700,
                letterSpacing: -0.3,
              ),
            ),
            const SizedBox(height: 14),
            FauxGlassCard.section(
              child: Column(
                children: [
                  _AccountActionRow(
                    icon: _SettingsIconKind.signOut,
                    iconColor: _SettingsIconColors.blue,
                    title: 'Sign out',
                    subtitle: 'Sign out on this device',
                    onTap: onSignOut,
                  ),
                  const Divider(
                    height: 24,
                    color: AppColors.glassBorderDim,
                  ),
                  _AccountActionRow(
                    icon: _SettingsIconKind.delete,
                    iconColor: AppColors.error,
                    title: 'Delete account',
                    subtitle: 'Permanently delete your Aura data',
                    onTap: onDeleteAccount,
                    destructive: true,
                  ),
                ],
              ),
            ),
          ],
        ),
      ),
    );
  }
}

class _AccountActionRow extends StatelessWidget {
  final _SettingsIconKind icon;
  final Color iconColor;
  final String title;
  final String subtitle;
  final VoidCallback onTap;
  final bool destructive;

  const _AccountActionRow({
    required this.icon,
    required this.iconColor,
    required this.title,
    required this.subtitle,
    required this.onTap,
    this.destructive = false,
  });

  @override
  Widget build(BuildContext context) {
    return GestureDetector(
      behavior: HitTestBehavior.opaque,
      onTap: onTap,
      child: SizedBox(
        height: _settingsTileHeight,
        child: Row(
          children: [
            _SettingsIcon(icon, color: iconColor),
            const SizedBox(width: 14),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    title,
                    style: TextStyle(
                      color: destructive
                          ? AppColors.error
                          : AppColors.textPrimary,
                      fontSize: 15,
                      fontWeight: FontWeight.w600,
                      height: 1.2,
                    ),
                  ),
                  const SizedBox(height: 2),
                  Text(
                    subtitle,
                    style: const TextStyle(
                      color: AppColors.textTertiary,
                      fontSize: 13,
                      height: 1.2,
                    ),
                  ),
                ],
              ),
            ),
            Icon(
              Icons.chevron_right_rounded,
              color: destructive ? AppColors.error : AppColors.textTertiary,
              size: 19,
            ),
          ],
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
