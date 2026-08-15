import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:go_router/go_router.dart';
import 'package:provider/provider.dart';

import '../../../core/constants/alarm_tones.dart';
import '../../../core/theme/app_colors.dart';
import '../../../core/theme/app_theme.dart';
import '../../../core/theme/glass_card.dart';
import '../../../data/services/alarm_service.dart';
import '../../viewmodels/auth_viewmodel.dart';
import '../../viewmodels/settings_viewmodel.dart';
import '../../widgets/pressable_tile.dart';
import 'alarm_tone_picker_screen.dart';

/// A device-local regular wake-up alarm.
///
/// Flutter owns only this editor. Saving crosses the MethodChannel once, then
/// Kotlin persists the definition and schedules every occurrence without
/// Flutter, Firestore, or the backend scheduler being alive.
class RegularAlarmScreen extends StatefulWidget {
  const RegularAlarmScreen({super.key});

  @override
  State<RegularAlarmScreen> createState() => _RegularAlarmScreenState();
}

class _RegularAlarmScreenState extends State<RegularAlarmScreen> {
  RegularAlarmSettings _alarm = const RegularAlarmSettings.defaults();
  bool _loading = true;
  bool _saving = false;
  bool _saved = false;
  String _deviceToneTitle = '';
  DateTime? _nextTriggerAt;

  static const _days = <({int value, String label})>[
    (value: 7, label: 'S'),
    (value: 1, label: 'M'),
    (value: 2, label: 'T'),
    (value: 3, label: 'W'),
    (value: 4, label: 'T'),
    (value: 5, label: 'F'),
    (value: 6, label: 'S'),
  ];

  @override
  void initState() {
    super.initState();
    unawaited(_load());
  }

  Future<void> _load() async {
    final service = context.read<AlarmService>();
    final values = await Future.wait<Object?>([
      service.regularAlarm(),
      service.deviceTone(),
      service.refreshCapabilities(),
    ]);
    unawaited(service.flushComingSoonInterest());
    if (!mounted) return;
    final alarm = values[0]! as RegularAlarmSettings;
    final deviceTone = values[1] as DeviceTone?;
    setState(() {
      _alarm = alarm;
      _deviceToneTitle = deviceTone?.title ?? '';
      _loading = false;
    });
  }

  void _replace(RegularAlarmSettings value) {
    HapticFeedback.selectionClick();
    setState(() {
      _alarm = value;
      _saved = false;
      _nextTriggerAt = null;
    });
  }

  void _adjustMinutes(int delta) {
    final total = (_alarm.hour * 60 + _alarm.minute + delta) % (24 * 60);
    final normalized = total < 0 ? total + 24 * 60 : total;
    _replace(_alarm.copyWith(hour: normalized ~/ 60, minute: normalized % 60));
  }

  Future<void> _pickTime() async {
    final picked = await showTimePicker(
      context: context,
      initialTime: TimeOfDay(hour: _alarm.hour, minute: _alarm.minute),
      helpText: 'Choose your wake-up time',
      builder: (context, child) => Theme(
        data: Theme.of(context).copyWith(
          colorScheme: Theme.of(context).colorScheme.copyWith(
            primary: AppColors.alarmAccent,
            surface: AppColors.surface,
            onSurface: AppColors.textPrimary,
          ),
        ),
        child: child!,
      ),
    );
    if (picked != null) {
      _replace(_alarm.copyWith(hour: picked.hour, minute: picked.minute));
    }
  }

  void _toggleDay(int day) {
    final next = {..._alarm.weekdays};
    if (!next.remove(day)) {
      next.add(day);
    } else if (next.isEmpty) {
      HapticFeedback.mediumImpact();
      _showMessage('Choose at least one day.');
      return;
    }
    _replace(_alarm.copyWith(weekdays: next));
  }

  Future<void> _pickSound() async {
    final selected = await context.push<String>(
      '/settings/alarm-sound',
      extra: AlarmTonePickerArgs(
        initialSlug: _alarm.tone,
        selectionOnly: true,
        // A local definition has no backend reminder id from which to render a
        // cached Buddy wake line. Every bundled ringtone remains available.
        allowBuddyVoice: false,
      ),
    );
    if (!mounted || selected == null) return;
    _replace(_alarm.copyWith(tone: selected));
    if (selected == kAlarmToneDevice) {
      final device = await context.read<AlarmService>().deviceTone();
      if (mounted) setState(() => _deviceToneTitle = device?.title ?? '');
    }
  }

  void _comingSoon(String feature, String label) {
    HapticFeedback.lightImpact();
    unawaited(context.read<AlarmService>().recordComingSoonInterest(feature));
    _showMessage('Thanks for the interest — $label is coming soon.');
  }

  void _openRoutines() {
    HapticFeedback.lightImpact();
    unawaited(
      context.read<AlarmService>().recordComingSoonInterest('routines'),
    );
    context.push('/settings/alarm/routines');
  }

  Future<void> _save() async {
    if (_saving) return;
    HapticFeedback.mediumImpact();
    setState(() => _saving = true);
    final service = context.read<AlarmService>();
    final result = await service.saveRegularAlarm(_alarm);
    if (!mounted) return;
    if (result == null) {
      setState(() => _saving = false);
      _showMessage("Couldn't save that alarm. Try once more.");
      return;
    }

    setState(() {
      _alarm = result.settings;
      _nextTriggerAt = result.nextTriggerAt;
      _saving = false;
      _saved = true;
    });

    // Keep Buddy-created alarms on the same explicit default when the user is
    // signed in. This is detached: the regular alarm above is already saved and
    // never waits for Firestore or a network round trip.
    if (context.read<AuthViewModel>().user != null) {
      unawaited(
        context
            .read<SettingsViewModel>()
            .selectAlarmTone(_alarm.tone)
            .then((_) => service.mirrorDefaultTone(_alarm.tone)),
      );
    }

    final caps = await service.refreshCapabilities();
    if (!mounted) return;
    if (_alarm.enabled && !caps.canRing) {
      ScaffoldMessenger.of(context)
        ..clearSnackBars()
        ..showSnackBar(
          SnackBar(
            content: const Text(
              'Saved. Android still needs alarm access for exact timing.',
            ),
            action: SnackBarAction(
              label: 'Turn on',
              textColor: AppColors.alarmAccent,
              onPressed: () => service.requestExactAlarmAccess(),
            ),
          ),
        );
    } else {
      _showMessage(_saveConfirmation());
    }
  }

  String _saveConfirmation() {
    if (!_alarm.enabled) return 'Alarm saved and turned off.';
    final next = _nextTriggerAt?.toLocal();
    if (next == null) return 'Alarm saved.';
    final day = _dayName(next.weekday);
    return 'Saved. Next alarm: $day at ${_formatTime(next.hour, next.minute)}.';
  }

  void _showMessage(String message) {
    if (!mounted) return;
    ScaffoldMessenger.of(context)
      ..clearSnackBars()
      ..showSnackBar(SnackBar(content: Text(message)));
  }

  String _dayName(int weekday) => const [
    'Monday',
    'Tuesday',
    'Wednesday',
    'Thursday',
    'Friday',
    'Saturday',
    'Sunday',
  ][weekday - 1];

  String _formatTime(int hour, int minute) {
    final twelveHour = hour % 12 == 0 ? 12 : hour % 12;
    final paddedMinute = minute.toString().padLeft(2, '0');
    return '$twelveHour:$paddedMinute ${hour < 12 ? 'AM' : 'PM'}';
  }

  String get _soundLabel {
    if (_alarm.tone == kAlarmToneDevice) {
      return _deviceToneTitle.isEmpty ? 'From your device' : _deviceToneTitle;
    }
    if (_alarm.tone == kAlarmToneSystemDefault) return 'Phone default';
    return alarmToneFor(_alarm.tone)?.label ?? 'Phone default';
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: AppColors.deepBackground,
      body: AmbientBackground(
        child: SafeArea(
          child: Column(
            children: [
              _Header(
                enabled: _alarm.enabled,
                onBack: () => Navigator.pop(context),
                onEnabledChanged: _loading
                    ? null
                    : (enabled) => _replace(_alarm.copyWith(enabled: enabled)),
              ),
              Expanded(
                child: AnimatedSwitcher(
                  duration: const Duration(milliseconds: 260),
                  child: _loading
                      ? const Center(
                          key: ValueKey('loading'),
                          child: CircularProgressIndicator(
                            color: AppColors.alarmAccent,
                            strokeWidth: 2,
                          ),
                        )
                      : _AlarmEditor(
                          key: const ValueKey('editor'),
                          alarm: _alarm,
                          days: _days,
                          soundLabel: _soundLabel,
                          onMinus: () => _adjustMinutes(-15),
                          onPlus: () => _adjustMinutes(15),
                          onTime: _pickTime,
                          onDay: _toggleDay,
                          onSound: _pickSound,
                          onVibrate: (value) =>
                              _replace(_alarm.copyWith(vibrate: value)),
                          onSunrise: () =>
                              _comingSoon('sunrise_alarm', 'Sunrise Alarm'),
                          onWeather: () => _comingSoon(
                            'weather_forecast',
                            'Weather forecast',
                          ),
                          onRoutines: _openRoutines,
                        ),
                ),
              ),
              if (!_loading)
                _SaveBar(
                  saving: _saving,
                  saved: _saved,
                  enabled: _alarm.enabled,
                  onSave: _save,
                ),
            ],
          ),
        ),
      ),
    );
  }
}

class _Header extends StatelessWidget {
  const _Header({
    required this.enabled,
    required this.onBack,
    required this.onEnabledChanged,
  });

  final bool enabled;
  final VoidCallback onBack;
  final ValueChanged<bool>? onEnabledChanged;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.fromLTRB(18, 10, 14, 6),
      child: Row(
        children: [
          GlassIconButton(
            icon: Icons.arrow_back_ios_new,
            onTap: onBack,
            iconSize: 17,
          ),
          const SizedBox(width: 14),
          Expanded(
            child: Text('Alarm', style: Theme.of(context).textTheme.titleLarge),
          ),
          Switch.adaptive(value: enabled, onChanged: onEnabledChanged),
        ],
      ),
    );
  }
}

class _AlarmEditor extends StatelessWidget {
  const _AlarmEditor({
    super.key,
    required this.alarm,
    required this.days,
    required this.soundLabel,
    required this.onMinus,
    required this.onPlus,
    required this.onTime,
    required this.onDay,
    required this.onSound,
    required this.onVibrate,
    required this.onSunrise,
    required this.onWeather,
    required this.onRoutines,
  });

  final RegularAlarmSettings alarm;
  final List<({int value, String label})> days;
  final String soundLabel;
  final VoidCallback onMinus;
  final VoidCallback onPlus;
  final VoidCallback onTime;
  final ValueChanged<int> onDay;
  final VoidCallback onSound;
  final ValueChanged<bool> onVibrate;
  final VoidCallback onSunrise;
  final VoidCallback onWeather;
  final VoidCallback onRoutines;

  @override
  Widget build(BuildContext context) {
    return TweenAnimationBuilder<double>(
      tween: Tween(begin: 0, end: 1),
      duration: const Duration(milliseconds: 420),
      curve: Curves.easeOutCubic,
      builder: (context, value, child) => Opacity(
        opacity: value,
        child: Transform.translate(
          offset: Offset(0, 18 * (1 - value)),
          child: child,
        ),
      ),
      child: ListView(
        padding: const EdgeInsets.fromLTRB(18, 10, 18, 24),
        children: [
          const Icon(
            Icons.alarm_rounded,
            color: AppColors.alarmAccent,
            size: 34,
          ),
          const SizedBox(height: 12),
          Text(
            'Set a regular wake-up alarm',
            textAlign: TextAlign.center,
            style: Theme.of(context).textTheme.displayMedium,
          ),
          const SizedBox(height: 22),
          AnimatedOpacity(
            opacity: alarm.enabled ? 1 : 0.55,
            duration: const Duration(milliseconds: 220),
            child: FauxGlassCard(
              borderRadius: 28,
              padding: const EdgeInsets.fromLTRB(16, 24, 16, 6),
              child: Column(
                children: [
                  Row(
                    mainAxisAlignment: MainAxisAlignment.spaceBetween,
                    children: [
                      _RoundControlButton(
                        icon: Icons.remove_rounded,
                        onTap: onMinus,
                      ),
                      PressableTile(
                        onTap: onTime,
                        pressedScale: 0.96,
                        child: _LargeTime(
                          hour: alarm.hour,
                          minute: alarm.minute,
                        ),
                      ),
                      _RoundControlButton(
                        icon: Icons.add_rounded,
                        onTap: onPlus,
                      ),
                    ],
                  ),
                  const SizedBox(height: 24),
                  Row(
                    mainAxisAlignment: MainAxisAlignment.spaceBetween,
                    children: [
                      for (final day in days)
                        _DayButton(
                          label: day.label,
                          selected: alarm.weekdays.contains(day.value),
                          onTap: () => onDay(day.value),
                        ),
                    ],
                  ),
                  const Padding(
                    padding: EdgeInsets.only(top: 20),
                    child: Divider(color: AppColors.divider),
                  ),
                  _OptionRow(
                    icon: Icons.wb_sunny_outlined,
                    title: 'Sunrise Alarm',
                    subtitle: 'Slowly brighten the screen before alarm',
                    trailing: const _ComingSoonIndicator(),
                    onTap: onSunrise,
                  ),
                  _OptionRow(
                    icon: Icons.notifications_active_outlined,
                    title: 'Sound',
                    subtitle: soundLabel,
                    trailing: const Icon(
                      Icons.chevron_right_rounded,
                      color: AppColors.textTertiary,
                    ),
                    onTap: onSound,
                  ),
                  _OptionRow(
                    icon: Icons.vibration_rounded,
                    title: 'Vibrate',
                    trailing: Switch.adaptive(
                      value: alarm.vibrate,
                      onChanged: onVibrate,
                    ),
                    onTap: () => onVibrate(!alarm.vibrate),
                  ),
                  _OptionRow(
                    icon: Icons.cloud_outlined,
                    title: 'Weather forecast',
                    subtitle: 'Show weather forecast after alarm',
                    trailing: const _ComingSoonIndicator(),
                    onTap: onWeather,
                  ),
                  _OptionRow(
                    icon: Icons.auto_awesome_outlined,
                    title: 'Routines',
                    subtitle: 'Start your morning with Buddy',
                    trailing: const _ComingSoonIndicator(showPlus: true),
                    onTap: onRoutines,
                    showDivider: false,
                  ),
                ],
              ),
            ),
          ),
        ],
      ),
    );
  }
}

class _LargeTime extends StatelessWidget {
  const _LargeTime({required this.hour, required this.minute});

  final int hour;
  final int minute;

  @override
  Widget build(BuildContext context) {
    final twelveHour = hour % 12 == 0 ? 12 : hour % 12;
    final value = '$twelveHour:${minute.toString().padLeft(2, '0')}';
    return Row(
      mainAxisSize: MainAxisSize.min,
      crossAxisAlignment: CrossAxisAlignment.end,
      children: [
        AnimatedSwitcher(
          duration: const Duration(milliseconds: 180),
          transitionBuilder: (child, animation) => FadeTransition(
            opacity: animation,
            child: ScaleTransition(scale: animation, child: child),
          ),
          child: Text(
            value,
            key: ValueKey(value),
            style: Theme.of(context).textTheme.displayLarge?.copyWith(
              fontSize: 50,
              fontWeight: FontWeight.w600,
              letterSpacing: -1.8,
            ),
          ),
        ),
        const SizedBox(width: 6),
        Padding(
          padding: const EdgeInsets.only(bottom: 7),
          child: Text(
            hour < 12 ? 'AM' : 'PM',
            style: AppTextStyles.monoLarge.copyWith(
              color: AppColors.textPrimary,
              fontWeight: FontWeight.w600,
            ),
          ),
        ),
      ],
    );
  }
}

class _RoundControlButton extends StatelessWidget {
  const _RoundControlButton({required this.icon, required this.onTap});

  final IconData icon;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    return PressableTile(
      onTap: onTap,
      pressedScale: 0.9,
      child: Container(
        width: 52,
        height: 52,
        decoration: BoxDecoration(
          shape: BoxShape.circle,
          color: AppColors.surfaceVariant,
          border: Border.all(color: AppColors.border),
        ),
        child: Icon(icon, color: AppColors.textSecondary, size: 28),
      ),
    );
  }
}

class _DayButton extends StatelessWidget {
  const _DayButton({
    required this.label,
    required this.selected,
    required this.onTap,
  });

  final String label;
  final bool selected;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    return PressableTile(
      onTap: onTap,
      pressedScale: 0.9,
      child: AnimatedContainer(
        duration: const Duration(milliseconds: 180),
        curve: Curves.easeOut,
        width: 38,
        height: 38,
        decoration: BoxDecoration(
          shape: BoxShape.circle,
          color: selected ? AppColors.alarmAccent : AppColors.surface,
          border: Border.all(
            color: selected ? AppColors.alarmAccent : AppColors.border,
          ),
          boxShadow: selected
              ? [
                  BoxShadow(
                    color: AppColors.alarmAccentGlow,
                    blurRadius: 12,
                    spreadRadius: 1,
                  ),
                ]
              : null,
        ),
        alignment: Alignment.center,
        child: Text(
          label,
          style: Theme.of(context).textTheme.labelLarge?.copyWith(
            color: selected ? AppColors.onAccent : AppColors.textSecondary,
          ),
        ),
      ),
    );
  }
}

class _OptionRow extends StatelessWidget {
  const _OptionRow({
    required this.icon,
    required this.title,
    required this.trailing,
    required this.onTap,
    this.subtitle,
    this.showDivider = true,
  });

  final IconData icon;
  final String title;
  final String? subtitle;
  final Widget trailing;
  final VoidCallback onTap;
  final bool showDivider;

  @override
  Widget build(BuildContext context) {
    return Column(
      children: [
        PressableTile(
          onTap: onTap,
          pressedScale: 0.985,
          child: Padding(
            padding: const EdgeInsets.symmetric(vertical: 14),
            child: Row(
              children: [
                Container(
                  width: 38,
                  height: 38,
                  decoration: BoxDecoration(
                    shape: BoxShape.circle,
                    color: AppColors.alarmAccentGlow,
                  ),
                  child: Icon(
                    icon,
                    color: AppColors.alarmAccent,
                    size: 21,
                  ),
                ),
                const SizedBox(width: 14),
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(
                        title,
                        style: Theme.of(context).textTheme.titleMedium,
                      ),
                      if (subtitle != null) ...[
                        const SizedBox(height: 3),
                        Text(
                          subtitle!,
                          style: Theme.of(context).textTheme.bodySmall,
                        ),
                      ],
                    ],
                  ),
                ),
                const SizedBox(width: 10),
                trailing,
              ],
            ),
          ),
        ),
        if (showDivider)
          const Padding(
            padding: EdgeInsets.only(left: 52),
            child: Divider(color: AppColors.divider),
          ),
      ],
    );
  }
}

class _ComingSoonIndicator extends StatelessWidget {
  const _ComingSoonIndicator({this.showPlus = false});

  final bool showPlus;

  @override
  Widget build(BuildContext context) {
    return Container(
      width: 34,
      height: 34,
      decoration: BoxDecoration(
        shape: BoxShape.circle,
        border: Border.all(color: AppColors.textTertiary, width: 1.5),
      ),
      alignment: Alignment.center,
      child: showPlus
          ? const Icon(
              Icons.add_rounded,
              color: AppColors.textTertiary,
              size: 21,
            )
          : Container(
              width: 8,
              height: 8,
              decoration: const BoxDecoration(
                shape: BoxShape.circle,
                color: AppColors.textTertiary,
              ),
            ),
    );
  }
}

class _SaveBar extends StatelessWidget {
  const _SaveBar({
    required this.saving,
    required this.saved,
    required this.enabled,
    required this.onSave,
  });

  final bool saving;
  final bool saved;
  final bool enabled;
  final VoidCallback onSave;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: EdgeInsets.fromLTRB(
        18,
        12,
        18,
        12 + MediaQuery.viewPaddingOf(context).bottom,
      ),
      decoration: const BoxDecoration(
        color: AppColors.deepBackground,
        border: Border(top: BorderSide(color: AppColors.divider)),
      ),
      child: SizedBox(
        width: double.infinity,
        height: 52,
        child: FilledButton(
          onPressed: saving ? null : onSave,
          style: FilledButton.styleFrom(
            backgroundColor: AppColors.alarmAccent,
            foregroundColor: AppColors.onAccent,
            disabledBackgroundColor: AppColors.surfaceVariant,
            disabledForegroundColor: AppColors.textDisabled,
            shape: RoundedRectangleBorder(
              borderRadius: BorderRadius.circular(26),
            ),
          ),
          child: AnimatedSwitcher(
            duration: const Duration(milliseconds: 180),
            child: saving
                ? const SizedBox(
                    key: ValueKey('saving'),
                    width: 20,
                    height: 20,
                    child: CircularProgressIndicator(
                      color: AppColors.onAccent,
                      strokeWidth: 2,
                    ),
                  )
                : Row(
                    key: ValueKey(saved ? 'saved' : 'save'),
                    mainAxisSize: MainAxisSize.min,
                    children: [
                      if (saved) ...[
                        const Icon(Icons.check_rounded, size: 19),
                        const SizedBox(width: 8),
                      ],
                      Text(
                        saved
                            ? 'Saved'
                            : enabled
                            ? 'Save alarm'
                            : 'Save as off',
                      ),
                    ],
                  ),
          ),
        ),
      ),
    );
  }
}
