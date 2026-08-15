import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:provider/provider.dart';

import '../../../core/theme/app_colors.dart';
import '../../../core/theme/glass_card.dart';
import '../../../data/services/alarm_service.dart';
import '../../widgets/pressable_tile.dart';

/// A faithful, non-functional preview of the planned post-alarm routine.
///
/// Every preview control records explicit interest, but none of them promises
/// or schedules behavior that the product does not ship yet.
class RoutineComingSoonScreen extends StatelessWidget {
  const RoutineComingSoonScreen({super.key});

  void _record(BuildContext context, String feature, String label) {
    HapticFeedback.lightImpact();
    unawaited(context.read<AlarmService>().recordComingSoonInterest(feature));
    ScaffoldMessenger.of(context)
      ..clearSnackBars()
      ..showSnackBar(
        SnackBar(
          content: Text('Thanks for the interest — $label is coming soon.'),
        ),
      );
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: AppColors.deepBackground,
      body: AmbientBackground(
        child: SafeArea(
          child: Column(
            children: [
              Padding(
                padding: const EdgeInsets.fromLTRB(18, 10, 14, 4),
                child: Row(
                  children: [
                    GlassIconButton(
                      icon: Icons.close_rounded,
                      onTap: () => Navigator.pop(context),
                    ),
                    const Spacer(),
                    GlassIconButton(
                      icon: Icons.more_vert_rounded,
                      onTap: () => _record(
                        context,
                        'routine_add_action',
                        'Routine customization',
                      ),
                    ),
                  ],
                ),
              ),
              Expanded(
                child: TweenAnimationBuilder<double>(
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
                    padding: const EdgeInsets.fromLTRB(20, 24, 20, 28),
                    children: [
                      Row(
                        children: [
                          const Icon(
                            Icons.alarm_rounded,
                            color: AppColors.alarmAccent,
                            size: 34,
                          ),
                          const SizedBox(width: 14),
                          Expanded(
                            child: Text(
                              'Voice assistant Routine',
                              style: Theme.of(context).textTheme.headlineSmall,
                            ),
                          ),
                        ],
                      ),
                      const SizedBox(height: 24),
                      Text(
                        'When I dismiss my alarm, this Routine will',
                        style: Theme.of(context).textTheme.titleMedium,
                      ),
                      const SizedBox(height: 18),
                      _RoutineAction(
                        icon: Icons.podcasts_rounded,
                        label: 'Tell me about the weather',
                        onTap: () => _record(
                          context,
                          'routine_weather',
                          'Weather in Routines',
                        ),
                      ),
                      _RoutineAction(
                        icon: Icons.podcasts_rounded,
                        label: "Tell me about today's calendar",
                        onTap: () => _record(
                          context,
                          'routine_calendar',
                          'Calendar in Routines',
                        ),
                      ),
                      _RoutineAction(
                        icon: Icons.podcasts_rounded,
                        label: "Tell me today's tasks",
                        onTap: () => _record(
                          context,
                          'routine_tasks',
                          'Tasks in Routines',
                        ),
                      ),
                      _RoutineAction(
                        icon: Icons.sentiment_very_satisfied_outlined,
                        label: 'Tell me a joke',
                        onTap: () => _record(
                          context,
                          'routine_joke',
                          'Jokes in Routines',
                        ),
                      ),
                      _RoutineAction(
                        icon: Icons.assistant_rounded,
                        label: 'Tell me about my commute',
                        onTap: () => _record(
                          context,
                          'routine_commute',
                          'Commute in Routines',
                        ),
                      ),
                      const SizedBox(height: 12),
                      Text(
                        'Media plays after other actions',
                        style: Theme.of(context).textTheme.bodyMedium,
                      ),
                      const SizedBox(height: 8),
                      _RoutineAction(
                        icon: Icons.play_circle_outline_rounded,
                        label: 'Play the news',
                        showHandle: false,
                        onTap: () => _record(
                          context,
                          'routine_news',
                          'News in Routines',
                        ),
                      ),
                      const SizedBox(height: 16),
                      Align(
                        alignment: Alignment.centerLeft,
                        child: OutlinedButton.icon(
                          onPressed: () => _record(
                            context,
                            'routine_add_action',
                            'Adding Routine actions',
                          ),
                          icon: const Icon(Icons.add_rounded),
                          label: const Text('Add action'),
                          style: OutlinedButton.styleFrom(
                            foregroundColor: AppColors.alarmAccentDark,
                            side: const BorderSide(color: AppColors.border),
                            padding: const EdgeInsets.symmetric(
                              horizontal: 18,
                              vertical: 14,
                            ),
                          ),
                        ),
                      ),
                      const SizedBox(height: 28),
                      Text(
                        'Suggested actions',
                        style: Theme.of(context).textTheme.titleMedium,
                      ),
                      const SizedBox(height: 8),
                      Text(
                        'More ways to start your morning will appear here.',
                        style: Theme.of(context).textTheme.bodySmall,
                      ),
                    ],
                  ),
                ),
              ),
              Container(
                width: double.infinity,
                padding: EdgeInsets.fromLTRB(
                  20,
                  12,
                  20,
                  12 + MediaQuery.viewPaddingOf(context).bottom,
                ),
                decoration: const BoxDecoration(
                  color: AppColors.deepBackground,
                  border: Border(top: BorderSide(color: AppColors.divider)),
                ),
                child: FilledButton(
                  onPressed: () =>
                      _record(context, 'routine_save', 'Alarm Routines'),
                  style: FilledButton.styleFrom(
                    backgroundColor: AppColors.alarmAccent,
                    foregroundColor: AppColors.onAccent,
                    minimumSize: const Size.fromHeight(52),
                  ),
                  child: const Text('Save'),
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}

class _RoutineAction extends StatelessWidget {
  const _RoutineAction({
    required this.icon,
    required this.label,
    required this.onTap,
    this.showHandle = true,
  });

  final IconData icon;
  final String label;
  final VoidCallback onTap;
  final bool showHandle;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 10),
      child: PressableTile(
        onTap: onTap,
        pressedScale: 0.985,
        child: FauxGlassCard(
          borderRadius: 18,
          padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 19),
          child: Row(
            children: [
              if (showHandle) ...[
                const Icon(
                  Icons.drag_handle_rounded,
                  color: AppColors.textTertiary,
                  size: 23,
                ),
                const SizedBox(width: 16),
              ],
              Icon(icon, color: AppColors.alarmAccent, size: 25),
              const SizedBox(width: 16),
              Expanded(
                child: Text(
                  label,
                  style: Theme.of(context).textTheme.titleMedium,
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}
