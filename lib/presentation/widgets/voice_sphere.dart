import 'dart:ui' as ui;

import 'package:flutter/material.dart';

/// Glowing voice sphere — the AI-provider speech-to-speech orb.
///
/// The body is a flowing teal / aqua / amber / warm-white gas (rendered by
/// `shaders/voice_sphere.frag`). Purely presentational (Widget Purity):
/// [intensity] is passed in by the screen.
///
/// [intensity] (0..1) drives motion speed + brightness only — the palette is
/// fixed: idle is calm and dim, active is bright and fast.
///
/// If the shader fails to load/compile on a device, it falls back to a
/// multi-color gradient sphere so the orb always renders.
class VoiceSphere extends StatefulWidget {
  final double intensity;
  final double size;

  const VoiceSphere({
    super.key,
    required this.intensity,
    this.size = 104,
  });

  @override
  State<VoiceSphere> createState() => _VoiceSphereState();
}

class _VoiceSphereState extends State<VoiceSphere>
    with SingleTickerProviderStateMixin {
  // Long period so the swirl's wrap-around seam is rarely visible. 
  // uTime is derived as controller.value * period, in seconds.
  static const int _periodSeconds = 120;

  late final AnimationController _controller;
  ui.FragmentShader? _shader;

  @override
  void initState() {
    super.initState();
    _controller = AnimationController(
      vsync: this,
      duration: const Duration(seconds: _periodSeconds),
    )..repeat();
    _loadShader();
  }

  Future<void> _loadShader() async {
    try {
      final program =
          await ui.FragmentProgram.fromAsset('shaders/voice_sphere.frag');
      if (!mounted) return;
      setState(() => _shader = program.fragmentShader());
    } catch (_) {
      // If shader unavailable on this device, _SpherePainter falls back to a
      // gradient sphere, so the orb still renders.
      if (mounted) setState(() => _shader = null);
    }
  }

  @override
  void dispose() {
    _controller.dispose();
    _shader?.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return RepaintBoundary(
      child: SizedBox(
        width: widget.size,
        height: widget.size,
        child: AnimatedBuilder(
          animation: _controller,
          builder: (_, _) {
            return CustomPaint(
              size: Size(widget.size, widget.size),
              painter: _SpherePainter(
                shader: _shader,
                time: _controller.value * _periodSeconds,
                intensity: widget.intensity.clamp(0.0, 1.0),
              ),
            );
          },
        ),
      ),
    );
  }
}

class _SpherePainter extends CustomPainter {
  final ui.FragmentShader? shader;
  final double time;
  final double intensity;

  _SpherePainter({
    required this.shader,
    required this.time,
    required this.intensity,
  });

  @override
  void paint(Canvas canvas, Size size) {
    final s = shader;
    if (s != null) {
      // Uniform indices match the declaration order in voice_sphere.frag.
      s.setFloat(0, time); // uTime
      s.setFloat(1, size.width); // uSize.x
      s.setFloat(2, size.height); // uSize.y
      s.setFloat(3, intensity); // uIntensity
      canvas.drawRect(Offset.zero & size, Paint()..shader = s);
      return;
    }
    _paintFallback(canvas, size);
  }

  // Multi-color gradient sphere used when the fragment shader is unavailable.
  // Palette mirrors the .frag, tuned for the cream theme.
  void _paintFallback(Canvas canvas, Size size) {
    const teal = Color(0xFF1ACCB0);
    const aqua = Color(0xFF4FA3C9);
    const amber = Color(0xFFE69A52);
    const warmWhite = Color(0xFFFDF7EC);
    final center = size.center(Offset.zero);
    final bodyRadius = size.width * 0.36;
    final glowAlpha = 0.7 + 0.3 * intensity;

    final haloPaint = Paint()
      ..shader = RadialGradient(
        colors: [
          teal.withValues(alpha: 0.7 * glowAlpha),
          teal.withValues(alpha: 0.0),
        ],
        stops: const [0.45, 1.0],
      ).createShader(
        Rect.fromCircle(center: center, radius: size.width * 0.5),
      );
    canvas.drawCircle(center, size.width * 0.5, haloPaint);

    final bodyPaint = Paint()
      ..shader = RadialGradient(
        center: const Alignment(-0.3, -0.4),
        colors: const [warmWhite, teal, aqua, amber],
        stops: const [0.0, 0.4, 0.72, 1.0],
      ).createShader(Rect.fromCircle(center: center, radius: bodyRadius));
    canvas.drawCircle(center, bodyRadius, bodyPaint);
  }

  @override
  bool shouldRepaint(covariant _SpherePainter old) =>
      old.time != time ||
      old.intensity != intensity ||
      old.shader != shader;
}
