import 'dart:math' as math;

import 'package:flutter/material.dart';

/// The bars that dance on a voice card while its preview clip plays.
///
/// This is NOT a visualisation of the audio. `just_audio` exposes no amplitude
/// stream, so nothing here is derived from the clip: it is a synthetic wave,
/// there to say "this one is talking" the way a recorder readout does. Do not
/// let a later reader believe these peaks mean something about the voice.
///
/// Motion is deliberately cheap. Eight of these sit on screen at once, so the
/// repeating ticker only runs while [active] (plus the short settle afterwards)
/// and the painter is isolated behind a [RepaintBoundary].
class VoiceWaveform extends StatefulWidget {
  final Color color;

  /// True while this voice's preview is audible.
  final bool active;

  final int barCount;

  const VoiceWaveform({
    super.key,
    required this.color,
    required this.active,
    this.barCount = 14,
  });

  @override
  State<VoiceWaveform> createState() => _VoiceWaveformState();
}

class _VoiceWaveformState extends State<VoiceWaveform>
    with TickerProviderStateMixin {
  /// Drives the travelling wave. Repeats only while there is something to show.
  late final AnimationController _wave = AnimationController(
    vsync: this,
    duration: const Duration(milliseconds: 1100),
  );

  /// A second, slower loop that swells and dips the whole wave. Without it the
  /// bars churn at a constant energy, which reads as a loading spinner rather
  /// than someone speaking.
  late final AnimationController _pulse = AnimationController(
    vsync: this,
    duration: const Duration(milliseconds: 2600),
  );

  /// Amplitude envelope. Ramps the bars up and, more importantly, lets them
  /// settle back to the flat idle line instead of snapping flat the instant the
  /// clip ends.
  late final AnimationController _envelope = AnimationController(
    vsync: this,
    duration: const Duration(milliseconds: 260),
    reverseDuration: const Duration(milliseconds: 220),
  );

  late final Animation<double> _amplitude = CurvedAnimation(
    parent: _envelope,
    curve: Curves.easeOutCubic,
    reverseCurve: Curves.easeInCubic,
  );

  @override
  void initState() {
    super.initState();
    if (widget.active) _start();
  }

  @override
  void didUpdateWidget(VoiceWaveform oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (widget.active == oldWidget.active) return;
    if (widget.active) {
      _start();
    } else {
      // Keep the wave turning through the settle, then stop the ticker so an
      // idle card costs nothing per frame.
      _envelope.reverse().whenComplete(() {
        if (mounted && !widget.active) {
          _wave.stop();
          _pulse.stop();
        }
      });
    }
  }

  void _start() {
    if (!_wave.isAnimating) _wave.repeat();
    if (!_pulse.isAnimating) _pulse.repeat();
    _envelope.forward();
  }

  @override
  void dispose() {
    _wave.dispose();
    _pulse.dispose();
    _envelope.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return RepaintBoundary(
      child: AnimatedBuilder(
        animation: Listenable.merge([_wave, _pulse, _amplitude]),
        builder: (context, _) => CustomPaint(
          size: Size.infinite,
          painter: _WavePainter(
            phase: _wave.value,
            pulse: _pulse.value,
            amplitude: _amplitude.value,
            color: widget.color,
            barCount: widget.barCount,
          ),
        ),
      ),
    );
  }
}

class _WavePainter extends CustomPainter {
  /// 0..1, one full loop of the travelling wave.
  final double phase;

  /// 0..1, one full loop of the slow swell that rides on top of it.
  final double pulse;

  /// 0 = resting line, 1 = fully talking.
  final double amplitude;

  final Color color;
  final int barCount;

  const _WavePainter({
    required this.phase,
    required this.pulse,
    required this.amplitude,
    required this.color,
    required this.barCount,
  });

  /// Per-bar rate and offset, derived from the index rather than drawn from a
  /// Random(): a rebuild must not reshuffle the wave mid-clip. The rates are
  /// mutually non-harmonic so the bars keep drifting out of step instead of
  /// locking into one marching pattern.
  static double _rate(int i) => 0.9 + ((i * 7) % 5) * 0.31;
  static double _offset(int i) => ((i * 3) % 7) / 7.0;

  @override
  void paint(Canvas canvas, Size size) {
    if (size.width <= 0 || size.height <= 0) return;

    const barWidth = 4.0;
    final gap = (size.width - barCount * barWidth) / (barCount - 1);
    final centerY = size.height / 2;
    final maxHeight = size.height;

    final paint = Paint()
      ..color = Color.lerp(
        color.withValues(alpha: 0.28),
        color.withValues(alpha: 0.8),
        amplitude,
      )!
      ..style = PaintingStyle.fill;

    // The whole wave breathes: loud stretches and near-pauses, the way speech
    // does. Never reaches 0, or the bars would look like they had stopped.
    final swell = 0.62 + 0.38 * (0.5 + 0.5 * math.sin(pulse * 2 * math.pi));

    for (var i = 0; i < barCount; i++) {
      // Taper the ends so the strip reads as a wave, not a bar chart.
      final edge = math.sin((i + 0.5) / barCount * math.pi);
      // At rest the bars sit as a shallow curve rather than a row of dots: a
      // waveform that is not playing, not a decorative dotted line.
      final restHeight = 3.0 + 5.0 * edge;

      final t = phase * _rate(i) + _offset(i);
      // Two harmonics, so a bar's peaks vary in size instead of every crest
      // reaching the same height.
      final swing = 0.5 +
          0.36 * math.sin(t * 2 * math.pi) +
          0.14 * math.sin(t * 4 * math.pi + _offset(i) * math.pi);
      final liveHeight = restHeight +
          (maxHeight - restHeight) * swing.clamp(0.0, 1.0) * edge * swell;

      final height = restHeight + (liveHeight - restHeight) * amplitude;
      final x = i * (barWidth + gap);
      canvas.drawRRect(
        RRect.fromRectAndRadius(
          Rect.fromLTWH(x, centerY - height / 2, barWidth, height),
          const Radius.circular(2),
        ),
        paint,
      );
    }
  }

  @override
  bool shouldRepaint(_WavePainter old) =>
      old.phase != phase ||
      old.pulse != pulse ||
      old.amplitude != amplitude ||
      old.color != color ||
      old.barCount != barCount;
}
