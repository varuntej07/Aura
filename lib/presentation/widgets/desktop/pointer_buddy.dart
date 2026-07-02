import 'dart:math' as math;

import 'package:flutter/material.dart';

import '../../../core/theme/app_colors.dart';

/// Buddy's pointer: a small teal orb that flies along a quadratic bezier arc
/// to the target, lands with a soft ring pulse, and shows the label bubble.
/// Purely presentational: position and label come in, nothing is read from
/// Provider (Widget Purity rule).
class PointerBuddy extends StatefulWidget {
  const PointerBuddy({
    super.key,
    required this.target,
    required this.label,
    this.flightDuration = const Duration(milliseconds: 900),
  });

  /// Window-local logical coordinates of the element being pointed at.
  final Offset target;
  final String label;
  final Duration flightDuration;

  @override
  State<PointerBuddy> createState() => _PointerBuddyState();
}

class _PointerBuddyState extends State<PointerBuddy>
    with SingleTickerProviderStateMixin {
  late final AnimationController _flight = AnimationController(
    vsync: this,
    duration: widget.flightDuration,
  )..forward();

  @override
  void dispose() {
    _flight.dispose();
    super.dispose();
  }

  /// Quadratic bezier from the top-center of the window (where the panel
  /// lives) to the target, arcing sideways so the flight reads as a hop, not
  /// a straight drop.
  Offset _positionAt(double t, Size windowSize) {
    final start = Offset(windowSize.width / 2, 24);
    final end = widget.target;
    final mid = Offset.lerp(start, end, 0.5)!;
    final perpendicular = Offset(-(end.dy - start.dy), end.dx - start.dx);
    final norm = perpendicular.distance == 0
        ? Offset.zero
        : perpendicular / perpendicular.distance;
    final control = mid + norm * math.min(160, (end - start).distance * 0.25);
    final oneMinusT = 1 - t;
    return start * (oneMinusT * oneMinusT) +
        control * (2 * oneMinusT * t) +
        end * (t * t);
  }

  @override
  Widget build(BuildContext context) {
    return LayoutBuilder(builder: (context, constraints) {
      final windowSize = Size(constraints.maxWidth, constraints.maxHeight);
      return AnimatedBuilder(
        animation: _flight,
        builder: (context, _) {
          final t = Curves.easeInOutCubic.transform(_flight.value);
          final position = _positionAt(t, windowSize);
          final landed = _flight.isCompleted;
          return Stack(
            children: [
              // Landing ring: pulses once at the target as the orb arrives.
              if (landed)
                Positioned(
                  left: widget.target.dx - 18,
                  top: widget.target.dy - 18,
                  child: _LandingRing(),
                ),
              Positioned(
                left: position.dx - 9,
                top: position.dy - 9,
                child: Container(
                  width: 18,
                  height: 18,
                  decoration: BoxDecoration(
                    color: AppColors.accentBase,
                    shape: BoxShape.circle,
                    boxShadow: [
                      BoxShadow(
                        color: AppColors.accentBase.withValues(alpha: 0.5),
                        blurRadius: 12,
                        spreadRadius: 2,
                      ),
                    ],
                  ),
                ),
              ),
              if (landed && widget.label.isNotEmpty)
                Positioned(
                  // Bubble sits above the target, nudged inside the window.
                  left: (widget.target.dx - 80)
                      .clamp(8.0, windowSize.width - 168),
                  top: (widget.target.dy - 56).clamp(8.0, windowSize.height),
                  child: _LabelBubble(label: widget.label),
                ),
            ],
          );
        },
      );
    });
  }
}

class _LandingRing extends StatelessWidget {
  @override
  Widget build(BuildContext context) {
    return TweenAnimationBuilder<double>(
      tween: Tween(begin: 0.4, end: 1.0),
      duration: const Duration(milliseconds: 500),
      curve: Curves.easeOut,
      builder: (context, t, _) => Opacity(
        opacity: 1 - t,
        child: Container(
          width: 36 * t + 12,
          height: 36 * t + 12,
          decoration: BoxDecoration(
            shape: BoxShape.circle,
            border: Border.all(color: AppColors.accentBase, width: 2),
          ),
        ),
      ),
    );
  }
}

class _LabelBubble extends StatelessWidget {
  const _LabelBubble({required this.label});

  final String label;

  @override
  Widget build(BuildContext context) {
    return TweenAnimationBuilder<double>(
      tween: Tween(begin: 0.0, end: 1.0),
      duration: const Duration(milliseconds: 250),
      builder: (context, t, child) => Opacity(opacity: t, child: child),
      child: Container(
        constraints: const BoxConstraints(maxWidth: 160),
        padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
        decoration: BoxDecoration(
          color: AppColors.background,
          borderRadius: BorderRadius.circular(16),
          border: Border.all(color: AppColors.glassBorderLight),
          boxShadow: const [
            BoxShadow(
              color: Color(0x2E3A3228),
              blurRadius: 16,
              offset: Offset(0, 4),
            ),
          ],
        ),
        child: Text(
          label,
          maxLines: 2,
          overflow: TextOverflow.ellipsis,
          style: const TextStyle(
            color: AppColors.textPrimary,
            fontSize: 13,
            fontWeight: FontWeight.w500,
          ),
        ),
      ),
    );
  }
}
