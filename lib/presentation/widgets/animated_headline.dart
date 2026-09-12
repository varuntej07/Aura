import 'package:flutter/material.dart';

/// A headline whose words rise and fade in one after another.
///
/// Hand-rolled, like every other animation in this app: there is no animation
/// package in the project, and the house pattern (see `shimmer_sweep.dart`) is
/// one controller, one `AnimatedBuilder`, and a `RepaintBoundary` around the
/// moving part.
///
/// Deliberately a per-word reveal rather than a typewriter. A typewriter holds
/// the reader on a half-finished sentence, and this line is the one that tells
/// someone who the app thinks they are; it should be readable almost at once and
/// merely arrive with some life.
class AnimatedHeadline extends StatefulWidget {
  final String text;
  final TextStyle style;

  /// Words start [stagger] apart, as a fraction of the whole run.
  final double stagger;

  /// How the words sit when they wrap to a second line.
  final WrapAlignment alignment;

  const AnimatedHeadline({
    super.key,
    required this.text,
    required this.style,
    this.stagger = 0.08,
    this.alignment = WrapAlignment.start,
  });

  @override
  State<AnimatedHeadline> createState() => _AnimatedHeadlineState();
}

class _AnimatedHeadlineState extends State<AnimatedHeadline>
    with SingleTickerProviderStateMixin {
  late final AnimationController _controller;

  @override
  void initState() {
    super.initState();
    _controller = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 620),
    );
  }

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    // Settled on the first frame when the user has asked for less motion; the
    // words are still all there, they just do not travel.
    if (MediaQuery.disableAnimationsOf(context)) {
      _controller.value = 1;
    } else if (!_controller.isAnimating && _controller.value == 0) {
      _controller.forward();
    }
  }

  @override
  void didUpdateWidget(covariant AnimatedHeadline oldWidget) {
    super.didUpdateWidget(oldWidget);
    // Replays only when the line actually changes (the name resolving, the hour
    // rolling over), never on an unrelated rebuild.
    if (oldWidget.text != widget.text &&
        !MediaQuery.disableAnimationsOf(context)) {
      _controller.forward(from: 0);
    }
  }

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final words = widget.text.split(' ').where((w) => w.isNotEmpty).toList();
    if (words.isEmpty) return const SizedBox.shrink();

    // Each word opens its window [stagger] after the previous one, and they all
    // finish together at the end of the run.
    final step = words.length > 1
        ? (widget.stagger).clamp(0.0, 1.0 / (words.length - 1))
        : 0.0;

    return RepaintBoundary(
      child: AnimatedBuilder(
        animation: _controller,
        builder: (context, _) {
          return Wrap(
            spacing: 0,
            alignment: widget.alignment,
            children: [
              for (var i = 0; i < words.length; i++)
                _Word(
                  text: i == words.length - 1 ? words[i] : '${words[i]} ',
                  style: widget.style,
                  t: Curves.easeOutCubic.transform(
                    Interval(
                      i * step,
                      (i * step + 0.55).clamp(0.0, 1.0),
                    ).transform(_controller.value),
                  ),
                ),
            ],
          );
        },
      ),
    );
  }
}

class _Word extends StatelessWidget {
  final String text;
  final TextStyle style;
  final double t;

  const _Word({required this.text, required this.style, required this.t});

  @override
  Widget build(BuildContext context) {
    return Opacity(
      opacity: t,
      child: Transform.translate(
        offset: Offset(0, 10 * (1 - t)),
        child: Text(text, style: style),
      ),
    );
  }
}
