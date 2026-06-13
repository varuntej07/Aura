import 'package:flutter/material.dart';

import '../../core/theme/app_colors.dart';
import '../../core/theme/glass_card.dart';

/// Centered empty-state starters for the Buddy chat. Renders up to 3 thin,
/// full-width tappable cards in the middle of the screen. 
/// Purely presentational: the personalized labels and the tap callback arrive via the constructor.
/// Tapping a card fills the message input (handled by the parent)
class ChatSuggestionPills extends StatelessWidget {
  const ChatSuggestionPills({
    super.key,
    required this.pills,
    required this.onTap,
  });

  final List<String> pills;
  final ValueChanged<String> onTap;

  @override
  Widget build(BuildContext context) {
    if (pills.isEmpty) return const SizedBox.shrink();
    final visible = pills.take(3).toList();

    return Center(
      child: SingleChildScrollView(
        padding: const EdgeInsets.symmetric(horizontal: 24, vertical: 24),
        child: _ShimmerSweep(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              for (final pill in visible)
                Padding(
                  padding: const EdgeInsets.only(bottom: 10),
                  child: GestureDetector(
                    onTap: () => onTap(pill),
                    child: FauxGlassCard(
                      borderRadius: 14,
                      padding: const EdgeInsets.symmetric(
                          horizontal: 16, vertical: 14),
                      child: Row(
                        children: [
                          Expanded(
                            child: Text(
                              pill,
                              style: const TextStyle(
                                color: AppColors.textPrimary,
                                fontSize: 14,
                                height: 1.35,
                                fontWeight: FontWeight.w500,
                              ),
                            ),
                          ),
                          const SizedBox(width: 8),
                          const Icon(
                            Icons.arrow_outward_rounded,
                            size: 15,
                            color: AppColors.textTertiary,
                          ),
                        ],
                      ),
                    ),
                  ),
                ),
            ],
          ),
        ),
      ),
    );
  }
}

/// Runs a single left-to-right light sweep across its child once, when the
/// pills first appear. One controller drives the whole stack so the band
/// glides over all the cards as one continuous flash (not per-card). The
/// `srcATop` blend paints the highlight only where the cards already have
/// pixels, so the gaps between cards stay dark and the streak reads as light
/// passing over glass.
class _ShimmerSweep extends StatefulWidget {
  const _ShimmerSweep({required this.child});

  final Widget child;

  @override
  State<_ShimmerSweep> createState() => _ShimmerSweepState();
}

class _ShimmerSweepState extends State<_ShimmerSweep>
    with SingleTickerProviderStateMixin {
  // Brighter than AppColors.glassHighlight (0x06FFFFFF is invisible as a flash).
  static const Color _sweepPeak = Color(0x4DFFFFFF);

  // Half-width of the band as a fraction of the sweep travel. Larger = thicker
  // flash. 0.28 gives a broad, clearly visible bar rather than a thin line.
  static const double _bandHalfWidth = 0.28;

  late final AnimationController _controller = AnimationController(
    vsync: this,
    duration: const Duration(milliseconds: 2100),
  )..forward();

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return AnimatedBuilder(
      animation: _controller,
      builder: (context, child) {
        // -1 -> 2 walks the band from off-left to off-right of the bounds.
        final t = _controller.value * 3 - 1;
        return ShaderMask(
          blendMode: BlendMode.srcATop,
          shaderCallback: (bounds) {
            return LinearGradient(
              begin: Alignment.centerLeft,
              end: Alignment.centerRight,
              colors: const [
                Colors.transparent,
                _sweepPeak,
                Colors.transparent,
              ],
              stops: [
                (t - _bandHalfWidth).clamp(0.0, 1.0),
                t.clamp(0.0, 1.0),
                (t + _bandHalfWidth).clamp(0.0, 1.0),
              ],
            ).createShader(bounds);
          },
          child: child,
        );
      },
      child: widget.child,
    );
  }
}
