import 'dart:async';

import 'package:flutter/material.dart';

import '../../core/theme/app_colors.dart';
import '../../data/services/backend_api_service.dart';

/// Compact inline chip embedded at the bottom of an assistant bubble when the
/// assistant called set_reminder. Shows message + time + live countdown.
/// Fades + slides up on first render to signal real-time creation.
class ReminderCard extends StatefulWidget {
  final ReminderPayload reminder;
  final VoidCallback onViewReminders;

  const ReminderCard({
    super.key,
    required this.reminder,
    required this.onViewReminders,
  });

  @override
  State<ReminderCard> createState() => _ReminderCardState();
}

class _ReminderCardState extends State<ReminderCard>
    with SingleTickerProviderStateMixin {
  late final AnimationController _ctrl;
  late final Animation<double> _fade;
  late final Animation<Offset> _slide;
  Timer? _ticker;
  Duration _remaining = Duration.zero;

  @override
  void initState() {
    super.initState();
    _ctrl = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 380),
    );
    _fade = CurvedAnimation(parent: _ctrl, curve: Curves.easeOut);
    _slide = Tween<Offset>(
      begin: const Offset(0, 0.25),
      end: Offset.zero,
    ).animate(CurvedAnimation(parent: _ctrl, curve: Curves.easeOutCubic));

    _tick();
    _ctrl.forward();

    _ticker = Timer.periodic(const Duration(seconds: 1), (_) {
      if (!mounted) return;
      setState(_tick);
      if (_remaining.isNegative) _ticker?.cancel();
    });
  }

  void _tick() {
    _remaining = widget.reminder.triggerAt.toLocal().difference(DateTime.now());
  }

  @override
  void dispose() {
    _ctrl.dispose();
    _ticker?.cancel();
    super.dispose();
  }

  String get _countdown {
    if (_remaining.isNegative || _remaining == Duration.zero) return 'now';
    final h = _remaining.inHours;
    final m = _remaining.inMinutes.remainder(60);
    final s = _remaining.inSeconds.remainder(60);
    if (h > 0) return '${h}h ${m}m';
    if (m > 0) return '${m}m ${s}s';
    return '${s}s';
  }

  String get _timeLabel {
    final local = widget.reminder.triggerAt.toLocal();
    final now = DateTime.now();
    final todayStart = DateTime(now.year, now.month, now.day);
    final localStart = DateTime(local.year, local.month, local.day);
    final dayDiff = localStart.difference(todayStart).inDays;

    final h = local.hour % 12 == 0 ? 12 : local.hour % 12;
    final m = local.minute.toString().padLeft(2, '0');
    final ampm = local.hour < 12 ? 'AM' : 'PM';
    final t = '$h:$m $ampm';

    if (dayDiff == 0) return t;
    if (dayDiff == 1) return 'Tomorrow · $t';
    const mo = [
      'Jan','Feb','Mar','Apr','May','Jun',
      'Jul','Aug','Sep','Oct','Nov','Dec',
    ];
    return '${mo[local.month - 1]} ${local.day} · $t';
  }

  @override
  Widget build(BuildContext context) {
    return FadeTransition(
      opacity: _fade,
      child: SlideTransition(
        position: _slide,
        child: GestureDetector(
          onTap: widget.onViewReminders,
          behavior: HitTestBehavior.opaque,
          child: Container(
            padding: const EdgeInsets.fromLTRB(10, 7, 10, 7),
            decoration: BoxDecoration(
              color: AppColors.accent.withValues(alpha: 0.07),
              borderRadius: BorderRadius.circular(7),
              border: const Border(
                left: BorderSide(color: AppColors.accent, width: 2),
              ),
            ),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              mainAxisSize: MainAxisSize.min,
              children: [
                Row(
                  children: [
                    const Icon(
                      Icons.alarm_outlined,
                      size: 13,
                      color: AppColors.accent,
                    ),
                    const SizedBox(width: 7),
                    Expanded(
                      child: Text(
                        widget.reminder.message,
                        style: const TextStyle(
                          color: AppColors.textSecondary,
                          fontSize: 12.5,
                          height: 1.2,
                        ),
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                      ),
                    ),
                  ],
                ),
                const SizedBox(height: 3),
                Row(
                  children: [
                    Text(
                      _timeLabel,
                      style: const TextStyle(
                        color: AppColors.textTertiary,
                        fontSize: 11,
                      ),
                    ),
                    if (!_remaining.isNegative) ...[
                      const Text(
                        ' · ',
                        style: TextStyle(color: AppColors.textTertiary, fontSize: 11),
                      ),
                      Text(
                        _countdown,
                        style: TextStyle(
                          color: AppColors.accentDark,
                          fontSize: 11,
                          fontWeight: FontWeight.w500,
                        ),
                      ),
                    ],
                  ],
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}
