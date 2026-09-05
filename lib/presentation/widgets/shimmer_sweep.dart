import 'package:flutter/material.dart';

/// Runs a band of light across its child.
///
/// The `srcATop` blend paints the highlight only where the child already has
/// pixels, so gaps between cards stay dark and the streak reads as light
/// passing over glass. A fully transparent child will not shimmer at all —
/// callers that want a sweep must give the subtree a fill first.
///
/// Defaults reproduce the original chat-suggestion behaviour exactly: one
/// left-to-right pass on appear.
class ShimmerSweep extends StatefulWidget {
  const ShimmerSweep({
    super.key,
    required this.child,
    this.begin = Alignment.centerLeft,
    this.end = Alignment.centerRight,
    this.repeat = false,
    this.duration = const Duration(milliseconds: 2100),
    this.peakColor = defaultPeak,
    this.bandHalfWidth = 0.28,
  });

  /// Brighter than AppColors.glassHighlight (0x06FFFFFF is invisible as a
  /// flash).
  static const Color defaultPeak = Color(0x4DFFFFFF);

  final Widget child;

  /// Direction of travel. `centerLeft`→`centerRight` sweeps horizontally;
  /// `bottomLeft`→`topRight` sweeps diagonally.
  final Alignment begin;
  final Alignment end;

  /// Loop forever instead of running a single pass on appear.
  final bool repeat;

  final Duration duration;
  final Color peakColor;

  /// Half-width of the band as a fraction of the sweep travel. Larger =
  /// thicker flash. 0.28 gives a broad, clearly visible bar rather than a
  /// thin line.
  final double bandHalfWidth;

  @override
  State<ShimmerSweep> createState() => _ShimmerSweepState();
}

class _ShimmerSweepState extends State<ShimmerSweep>
    with SingleTickerProviderStateMixin {
  late final AnimationController _controller = AnimationController(
    vsync: this,
    duration: widget.duration,
  );

  @override
  void initState() {
    super.initState();
    if (widget.repeat) {
      _controller.repeat();
    } else {
      _controller.forward();
    }
  }

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    // Isolated so a looping sweep repaints only its own subtree, matching how
    // voice_sphere and voice_waveform fence their continuous painters.
    return RepaintBoundary(
      child: AnimatedBuilder(
        animation: _controller,
        builder: (context, child) {
          // -1 -> 2 walks the band from off-left to off-right of the bounds.
          // The off-canvas stretches at either end are what make a repeating
          // sweep read as a periodic flash instead of a constant strobe.
          final t = _controller.value * 3 - 1;
          return ShaderMask(
            blendMode: BlendMode.srcATop,
            shaderCallback: (bounds) {
              return LinearGradient(
                begin: widget.begin,
                end: widget.end,
                colors: [
                  Colors.transparent,
                  widget.peakColor,
                  Colors.transparent,
                ],
                stops: [
                  (t - widget.bandHalfWidth).clamp(0.0, 1.0),
                  t.clamp(0.0, 1.0),
                  (t + widget.bandHalfWidth).clamp(0.0, 1.0),
                ],
              ).createShader(bounds);
            },
            child: child,
          );
        },
        // Passed through so the subtree is not rebuilt every frame.
        child: widget.child,
      ),
    );
  }
}
