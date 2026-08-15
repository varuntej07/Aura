import 'package:flutter/material.dart';
import 'package:just_audio/just_audio.dart';
import 'package:provider/provider.dart';

import '../../../core/constants/alarm_tones.dart';
import '../../../core/logging/app_logger.dart';
import '../../../core/theme/app_colors.dart';
import '../../../core/theme/glass_card.dart';
import '../../../data/services/alarm_service.dart';
import '../../viewmodels/auth_viewmodel.dart';
import '../../viewmodels/settings_viewmodel.dart';

/// Lets the user choose what wakes them.
///
/// Previews are the SAME files the alarm plays: `assets/alarm_tones/<slug>.ogg`
/// is bundled once and read by Kotlin out of the APK at ring time, so what is
/// auditioned here cannot drift from what actually goes off at 6 AM.
///
/// The device row is deliberately last and deliberately different. It hands off
/// to the OS picker, and the URI it returns stays on the phone: it points into
/// the user's own storage, would mean nothing on another device, and only the
/// `device` slug itself is ever synced.
class AlarmTonePickerScreen extends StatefulWidget {
  const AlarmTonePickerScreen({super.key});

  @override
  State<AlarmTonePickerScreen> createState() => _AlarmTonePickerScreenState();
}

class _AlarmTonePickerScreenState extends State<AlarmTonePickerScreen> {
  final AudioPlayer _player = AudioPlayer();
  String? _playingSlug;
  DeviceTone? _deviceTone;

  @override
  void initState() {
    super.initState();
    _loadDeviceTone();
  }

  @override
  void dispose() {
    _player.dispose();
    super.dispose();
  }

  Future<void> _loadDeviceTone() async {
    final tone = await context.read<AlarmService>().deviceTone();
    if (mounted) setState(() => _deviceTone = tone);
  }

  Future<void> _preview(AlarmTone tone) async {
    // Tapping the tone that is already playing stops it. Restarting the same
    // loop is never what that tap means.
    if (_playingSlug == tone.slug) {
      await _player.stop();
      if (mounted) setState(() => _playingSlug = null);
      return;
    }

    setState(() => _playingSlug = tone.slug);
    try {
      await _player.stop();
      await _player.setAsset(tone.previewAsset);
      // Looped, because these are loops and a tone is judged on how it feels
      // the fourth time round rather than the first.
      await _player.setLoopMode(LoopMode.one);
      await _player.play();
    } catch (e, st) {
      AppLogger.error(
        'Alarm tone preview failed for ${tone.slug}',
        error: e,
        stackTrace: st,
        tag: 'AlarmTonePicker',
      );
      if (mounted) {
        setState(() => _playingSlug = null);
        ScaffoldMessenger.of(context).showSnackBar(
          const SnackBar(content: Text("Couldn't play that one.")),
        );
      }
    }
  }

  Future<void> _choose(AlarmTone tone) async {
    await _stopPreview();
    if (!mounted) return;
    await _persistSelection(tone.slug);
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(content: Text(_confirmationFor(tone))),
    );
  }

  String _confirmationFor(AlarmTone tone) => tone.slug == kAlarmToneBuddy
      ? "Done. I'll wake you myself."
      : "Done. ${tone.label} it is.";

  Future<void> _chooseDevice() async {
    await _stopPreview();
    if (!mounted) return;
    final service = context.read<AlarmService>();
    final picked = await service.pickDeviceTone();
    if (!mounted) return;
    if (picked == null) {
      // Backing out of the picker is not a failure and gets no message.
      return;
    }
    setState(() => _deviceTone = picked);
    await _persistSelection(kAlarmToneDevice);
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(content: Text('Done. ${picked.title} it is.')),
    );
  }

  Future<void> _chooseSystemDefault() async {
    await _stopPreview();
    if (!mounted) return;
    await _persistSelection(kAlarmToneSystemDefault);
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(
      const SnackBar(content: Text("Done. Your phone's own alarm sound.")),
    );
  }

  Future<void> _persistSelection(String slug) async {
    await context.read<SettingsViewModel>().selectAlarmTone(slug);
    if (!mounted) return;
    await context.read<AlarmService>().mirrorDefaultTone(slug);
  }

  Future<void> _stopPreview() async {
    if (_playingSlug == null) return;
    await _player.stop();
    if (mounted) setState(() => _playingSlug = null);
  }

  @override
  Widget build(BuildContext context) {
    final settingsVm = context.watch<SettingsViewModel>();
    final signedIn = context.watch<AuthViewModel>().user != null;
    final selected = displayAlarmToneSlug(settingsVm.settings?.alarmTone);

    return Scaffold(
      backgroundColor: Colors.transparent,
      body: AmbientBackground(
        child: SafeArea(
          child: Column(
            children: [
              Padding(
                padding: const EdgeInsets.symmetric(horizontal: 20, vertical: 12),
                child: Row(
                  children: [
                    GlassIconButton(
                      icon: Icons.arrow_back_ios_new,
                      onTap: () => Navigator.pop(context),
                      iconSize: 17,
                    ),
                    const SizedBox(width: 14),
                    const Text(
                      'Alarm sound',
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
              Expanded(
                child: ListView(
                  padding: const EdgeInsets.fromLTRB(16, 4, 16, 32),
                  children: [
                    const Padding(
                      padding: EdgeInsets.fromLTRB(4, 0, 4, 16),
                      child: Text(
                        'Tap play to hear one, tap the card to keep it. This is '
                        'the default for every alarm, unless you ask me for '
                        'something else when you set one.',
                        style: TextStyle(
                          color: AppColors.textTertiary,
                          fontSize: 13,
                          height: 1.4,
                        ),
                      ),
                    ),
                    for (final tone in kAlarmTones)
                      Padding(
                        padding: const EdgeInsets.only(bottom: 10),
                        child: _ToneCard(
                          tone: tone,
                          selected: signedIn && tone.slug == selected,
                          playing: _playingSlug == tone.slug,
                          onPreview: () => _preview(tone),
                          onChoose: signedIn ? () => _choose(tone) : null,
                        ),
                      ),
                    const SizedBox(height: 6),
                    _PlainRow(
                      icon: Icons.library_music_outlined,
                      title: _deviceTone?.title.isNotEmpty == true
                          ? _deviceTone!.title
                          : 'Choose from device',
                      subtitle: _deviceTone?.title.isNotEmpty == true
                          ? 'From your own sounds. Tap to change.'
                          : 'Pick any alarm sound already on your phone.',
                      selected: signedIn && selected == kAlarmToneDevice,
                      onTap: signedIn ? _chooseDevice : null,
                    ),
                    const SizedBox(height: 10),
                    _PlainRow(
                      icon: Icons.phone_android_outlined,
                      title: 'Phone default',
                      subtitle: "Whatever your phone's own alarm sound is.",
                      selected: signedIn && selected == kAlarmToneSystemDefault,
                      onTap: signedIn ? _chooseSystemDefault : null,
                    ),
                    if (!signedIn)
                      const Padding(
                        padding: EdgeInsets.fromLTRB(4, 20, 4, 0),
                        child: Text(
                          'Sign in to keep a sound.',
                          style: TextStyle(
                            color: AppColors.textTertiary,
                            fontSize: 13,
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

class _ToneCard extends StatelessWidget {
  final AlarmTone tone;
  final bool selected;
  final bool playing;
  final VoidCallback onPreview;
  final VoidCallback? onChoose;

  const _ToneCard({
    required this.tone,
    required this.selected,
    required this.playing,
    required this.onPreview,
    required this.onChoose,
  });

  @override
  Widget build(BuildContext context) {
    final tint = tone.tint;
    // Fills are light so the cream shows through; the icon needs a darkened
    // version of the same hue to stay legible on it.
    final inkTint = Color.lerp(tint, Colors.black, 0.42)!;

    return GestureDetector(
      onTap: onChoose,
      child: FauxGlassCard(
        borderRadius: 20,
        padding: const EdgeInsets.all(14),
        borderColor: tint.withValues(alpha: selected ? 0.65 : 0.24),
        gradient: LinearGradient(
          begin: Alignment.topLeft,
          end: Alignment.bottomRight,
          colors: [
            tint.withValues(alpha: 0.16),
            tint.withValues(alpha: 0.05),
          ],
        ),
        child: Row(
          children: [
            GestureDetector(
              onTap: onPreview,
              child: Container(
                width: 44,
                height: 44,
                decoration: BoxDecoration(
                  color: tint.withValues(alpha: playing ? 0.34 : 0.18),
                  shape: BoxShape.circle,
                ),
                child: Icon(
                  playing ? Icons.stop_rounded : Icons.play_arrow_rounded,
                  size: 24,
                  color: inkTint,
                ),
              ),
            ),
            const SizedBox(width: 14),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    tone.label,
                    style: const TextStyle(
                      color: AppColors.textPrimary,
                      fontSize: 16,
                      fontWeight: FontWeight.w600,
                    ),
                  ),
                  const SizedBox(height: 3),
                  Text(
                    tone.blurb,
                    style: const TextStyle(
                      color: AppColors.textTertiary,
                      fontSize: 12.5,
                      height: 1.3,
                    ),
                  ),
                ],
              ),
            ),
            if (selected) ...[
              const SizedBox(width: 8),
              Icon(Icons.check_circle_rounded, size: 22, color: inkTint),
            ],
          ],
        ),
      ),
    );
  }
}

/// The two rows that are not a bundled tone: the device picker and the phone's
/// own default. Untinted, because neither has a sound this app designed.
class _PlainRow extends StatelessWidget {
  final IconData icon;
  final String title;
  final String subtitle;
  final bool selected;
  final VoidCallback? onTap;

  const _PlainRow({
    required this.icon,
    required this.title,
    required this.subtitle,
    required this.selected,
    required this.onTap,
  });

  @override
  Widget build(BuildContext context) {
    return GestureDetector(
      onTap: onTap,
      child: FauxGlassCard(
        borderRadius: 20,
        padding: const EdgeInsets.all(14),
        borderColor: AppColors.border.withValues(alpha: selected ? 0.9 : 0.4),
        child: Row(
          children: [
            SizedBox(
              width: 44,
              child: Icon(icon, size: 22, color: AppColors.textSecondary),
            ),
            const SizedBox(width: 14),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    title,
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                    style: const TextStyle(
                      color: AppColors.textPrimary,
                      fontSize: 16,
                      fontWeight: FontWeight.w600,
                    ),
                  ),
                  const SizedBox(height: 3),
                  Text(
                    subtitle,
                    style: const TextStyle(
                      color: AppColors.textTertiary,
                      fontSize: 12.5,
                      height: 1.3,
                    ),
                  ),
                ],
              ),
            ),
            if (selected) ...[
              const SizedBox(width: 8),
              const Icon(
                Icons.check_circle_rounded,
                size: 22,
                color: AppColors.textSecondary,
              ),
            ],
          ],
        ),
      ),
    );
  }
}
