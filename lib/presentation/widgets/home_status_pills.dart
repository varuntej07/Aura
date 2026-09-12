import 'package:flutter/material.dart';
import 'package:flutter/scheduler.dart';
import 'package:provider/provider.dart';

import '../../core/theme/app_colors.dart';
import '../../core/theme/glass_card.dart';
import '../viewmodels/home_deck_viewmodel.dart';
import 'shimmer_sweep.dart';

/// What is already running for you, rolling past above the deck.
///
/// The cards below are things you could start; these are things that are already
/// true. That distinction is the whole reason they are a different shape: a pill
/// is a status, not an offer, and tapping one goes to where that thing lives.
class HomeStatusPills extends StatelessWidget {
  /// Where a pill sends the user. Routing stays with the screen so this widget
  /// keeps knowing nothing about GoRouter.
  final void Function(String route) onNavigate;

  /// A watched topic opens a chat with Buddy speaking first about it.
  final void Function(String openingMessage) onOpenTracker;

  const HomeStatusPills({
    super.key,
    required this.onNavigate,
    required this.onOpenTracker,
  });

  @override
  Widget build(BuildContext context) {
    final status = context.watch<HomeDeckViewModel>().status;
    // Nothing running yet renders nothing at all. An empty strip is worse than
    // no strip: it reserves space to say the app has nothing for you.
    if (status.isEmpty) return const SizedBox.shrink();

    final pills = <Widget>[
      if (status.briefingReady)
        _Pill(
          label: 'Morning briefing ready',
          color: AppColors.accentBase,
          onTap: () => onNavigate('/briefing'),
        ),
      if (status.remindersToday > 0)
        _Pill(
          label: status.remindersToday == 1
              ? '1 reminder today'
              : '${status.remindersToday} reminders today',
          color: AppColors.premium,
          onTap: () => onNavigate('/reminders'),
        ),
      for (final ritual in status.rituals)
        _Pill(
          label: ritual.pillLabel,
          color: AppColors.accentBase,
          onTap: () => onNavigate('/reminders'),
        ),
      for (final tracker in status.trackers)
        _Pill(
          label: 'Watching ${tracker.title}',
          color: const Color(0xFF8391C4),
          onTap: () => onOpenTracker(
            "I'm keeping an eye on ${tracker.title} for you. Want the latest?",
          ),
        ),
    ];

    return SizedBox(
      height: 38,
      child: _PillTicker(pills: pills),
    );
  }
}

/// The pills, rolling past like a news ticker.
///
/// Built on an endless horizontal ListView rather than a translated Row, because
/// a Row would have to be measured before it could be wrapped seamlessly, and a
/// half-measured first frame is exactly when the user is looking. The list hands
/// out `pills[index % pills.length]` forever, so there is no seam to hide and no
/// width to know.
class _PillTicker extends StatefulWidget {
  final List<Widget> pills;

  const _PillTicker({required this.pills});

  @override
  State<_PillTicker> createState() => _PillTickerState();
}

class _PillTickerState extends State<_PillTicker>
    with SingleTickerProviderStateMixin {
  /// Logical pixels a second. Slow enough to read a pill as it passes.
  static const double _speed = 26;

  final ScrollController _scroll = ScrollController();
  Ticker? _ticker;
  Duration _last = Duration.zero;

  @override
  void initState() {
    super.initState();
    // Nothing starts until the list has laid out, or the first jumpTo throws.
    WidgetsBinding.instance.addPostFrameCallback((_) => _start());
  }

  void _start() {
    if (!mounted || _ticker != null) return;
    // A ticker that never stops is the wrong default on a screen someone leaves
    // running, so it also stands down when the platform asks for less motion.
    if (MediaQuery.disableAnimationsOf(context)) return;
    _ticker = createTicker(_onTick)..start();
  }

  void _onTick(Duration elapsed) {
    final delta = elapsed - _last;
    _last = elapsed;
    if (!_scroll.hasClients) return;
    // Frame-time based, not per-frame constant: a dropped frame moves the strip
    // by the time that actually passed instead of stuttering.
    final next = _scroll.offset + _speed * delta.inMicroseconds / 1e6;
    _scroll.jumpTo(next);
  }

  @override
  void dispose() {
    _ticker?.dispose();
    _scroll.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final pills = widget.pills;
    if (pills.isEmpty) return const SizedBox.shrink();

    return RepaintBoundary(
      child: ShimmerSweep(
        child: ListView.builder(
          controller: _scroll,
          scrollDirection: Axis.horizontal,
          // The strip drives itself; a finger dragging it would fight the ticker
          // and leave it parked wherever the drag ended.
          physics: const NeverScrollableScrollPhysics(),
          padding: const EdgeInsets.symmetric(horizontal: 16),
          itemBuilder: (context, index) => Padding(
            padding: const EdgeInsets.only(right: 8),
            child: pills[index % pills.length],
          ),
        ),
      ),
    );
  }
}

class _Pill extends StatelessWidget {
  final String label;
  final Color color;
  final VoidCallback onTap;

  const _Pill({
    required this.label,
    required this.color,
    required this.onTap,
  });

  @override
  Widget build(BuildContext context) {
    return GestureDetector(
      onTap: onTap,
      behavior: HitTestBehavior.opaque,
      child: Center(
        child: FauxGlassCard(
          borderRadius: 20,
          padding: const EdgeInsets.fromLTRB(10, 8, 14, 8),
          child: Row(
            mainAxisSize: MainAxisSize.min,
            children: [
              Container(
                width: 7,
                height: 7,
                decoration: BoxDecoration(
                  shape: BoxShape.circle,
                  color: color,
                  boxShadow: [
                    BoxShadow(
                      color: color.withValues(alpha: 0.28),
                      blurRadius: 0,
                      spreadRadius: 3,
                    ),
                  ],
                ),
              ),
              const SizedBox(width: 9),
              Text(
                label,
                style: const TextStyle(
                  fontSize: 12.5,
                  fontWeight: FontWeight.w500,
                  color: AppColors.textPrimary,
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}
