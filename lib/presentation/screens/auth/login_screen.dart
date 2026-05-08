import 'dart:math' as math;
import 'dart:ui' as ui;
import 'package:flutter/gestures.dart';
import 'package:flutter/material.dart';
import 'package:google_fonts/google_fonts.dart';
import 'package:provider/provider.dart';
import 'package:url_launcher/url_launcher.dart';

import '../../viewmodels/auth_viewmodel.dart';
import '../../widgets/error_display.dart';
import '../../widgets/loading_indicator.dart';

// Orbital particle data

class _OrbitalParticle {
  final double radiusX;
  final double radiusY;
  final double phase;
  final double angularSpeed;
  final double dotSize;
  final double maxOpacity;
  final double hue; // 0–360

  const _OrbitalParticle({
    required this.radiusX,
    required this.radiusY,
    required this.phase,
    required this.angularSpeed,
    required this.dotSize,
    required this.maxOpacity,
    required this.hue,
  });
}

// Black hole CustomPainter

class _BlackHolePainter extends CustomPainter {
  final double t;

  static const int _trailSteps = 30;
  static const double _trailArcSpan = 0.65;

  static final List<_OrbitalParticle> _particles = _buildParticles();

  static List<_OrbitalParticle> _buildParticles() {
    final rng = math.Random(42);
    const bandBaseRadii = [70.0, 110.0, 155.0, 200.0];
    const bandCounts = [15, 20, 22, 18];
    final result = <_OrbitalParticle>[];
    for (int b = 0; b < bandBaseRadii.length; b++) {
      for (int i = 0; i < bandCounts[b]; i++) {
        final rx = bandBaseRadii[b] + (rng.nextDouble() - 0.5) * 18;
        final ry = rx * (0.24 + rng.nextDouble() * 0.10);
        result.add(_OrbitalParticle(
          radiusX: rx,
          radiusY: ry,
          phase: rng.nextDouble() * math.pi * 2,
          angularSpeed: 0.4 + rng.nextDouble() * 0.5 + (3 - b) * 0.25,
          dotSize: 0.8 + rng.nextDouble() * 1.4,
          maxOpacity: 0.25 + rng.nextDouble() * 0.50,
          hue: 230 + rng.nextDouble() * 60, // blue-indigo-violet
        ));
      }
    }
    return result;
  }

  _BlackHolePainter(this.t);

  @override
  void paint(Canvas canvas, Size size) {
    final center = Offset(size.width / 2, size.height * 0.42);
    _drawSpaceBackground(canvas, size, center);
    _drawOrbitalParticles(canvas, center);
    _drawAccretionRing(canvas, center);
    _drawSingularity(canvas, center);
  }

  void _drawSpaceBackground(Canvas canvas, Size size, Offset center) {
    final rect = Rect.fromLTWH(0, 0, size.width, size.height);
    canvas.drawRect(rect, Paint()..color = const Color(0xFF04040F));
    final glowPaint = Paint()
      ..shader = RadialGradient(
        colors: [const Color(0xFF0D0D2B), const Color(0xFF04040F)],
        stops: const [0.0, 1.0],
      ).createShader(Rect.fromCircle(center: center, radius: size.width * 0.55));
    canvas.drawRect(rect, glowPaint);
  }

  void _drawOrbitalParticles(Canvas canvas, Offset center) {
    for (final p in _particles) {
      final angle = p.phase + p.angularSpeed * t * math.pi * 2;
      final stepSize = _trailArcSpan / _trailSteps;
      for (int i = 0; i < _trailSteps; i++) {
        final trailAngle = angle - i * stepSize;
        final fade = 1.0 - i / _trailSteps;
        final opacity = (p.maxOpacity * fade * fade).clamp(0.0, 1.0);
        final x = center.dx + p.radiusX * math.cos(trailAngle);
        final y = center.dy + p.radiusY * math.sin(trailAngle);
        canvas.drawCircle(
          Offset(x, y),
          p.dotSize * (0.4 + 0.6 * fade),
          Paint()
            ..color = HSVColor.fromAHSV(opacity, p.hue, 0.65, 0.95).toColor(),
        );
      }
    }
  }

  void _drawAccretionRing(Canvas canvas, Offset center) {
    const double rx = 105;
    const double ry = 29;
    final rect = Rect.fromCenter(center: center, width: rx * 2, height: ry * 2);

    canvas.drawOval(
      rect,
      Paint()
        ..color = const Color(0xFF6B5BD2).withValues(alpha: 0.12)
        ..style = PaintingStyle.stroke
        ..strokeWidth = 14
        ..maskFilter = const MaskFilter.blur(BlurStyle.normal, 14),
    );
    canvas.drawOval(
      rect,
      Paint()
        ..color = const Color(0xFF7C6FCD).withValues(alpha: 0.22)
        ..style = PaintingStyle.stroke
        ..strokeWidth = 5
        ..maskFilter = const MaskFilter.blur(BlurStyle.normal, 6),
    );
    canvas.drawOval(
      rect,
      Paint()
        ..color = const Color(0xFF9D8FF0).withValues(alpha: 0.55)
        ..style = PaintingStyle.stroke
        ..strokeWidth = 1.2,
    );
  }

  void _drawSingularity(Canvas canvas, Offset center) {
    canvas.drawCircle(
      center,
      52,
      Paint()
        ..shader = RadialGradient(
          colors: [Colors.black, Colors.transparent],
          stops: const [0.55, 1.0],
        ).createShader(Rect.fromCircle(center: center, radius: 52)),
    );
  }

  @override
  bool shouldRepaint(_BlackHolePainter old) => old.t != t;
}

// ── Screen ────────────────────────────────────────────────────────────────────

class LoginScreen extends StatefulWidget {
  const LoginScreen({super.key});

  @override
  State<LoginScreen> createState() => _LoginScreenState();
}

class _LoginScreenState extends State<LoginScreen>
    with TickerProviderStateMixin {
  late final AnimationController _orbitController;
  late final AnimationController _fadeController;
  late final Animation<double> _iconFade;
  late final Animation<double> _nameFade;
  late final Animation<double> _taglineFade;
  late final Animation<double> _buttonsFade;

  final _emailController = TextEditingController();
  final _passwordController = TextEditingController();
  bool _showEmailForm = false;
  bool _obscurePassword = true;

  @override
  void initState() {
    super.initState();
    _orbitController = AnimationController(
      vsync: this,
      duration: const Duration(seconds: 18),
    )..repeat();
    _fadeController = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 1600),
    )..forward();
    _iconFade = CurvedAnimation(
      parent: _fadeController,
      curve: const Interval(0.0, 0.4, curve: Curves.easeOut),
    );
    _nameFade = CurvedAnimation(
      parent: _fadeController,
      curve: const Interval(0.2, 0.6, curve: Curves.easeOut),
    );
    _taglineFade = CurvedAnimation(
      parent: _fadeController,
      curve: const Interval(0.4, 0.8, curve: Curves.easeOut),
    );
    _buttonsFade = CurvedAnimation(
      parent: _fadeController,
      curve: const Interval(0.6, 1.0, curve: Curves.easeOut),
    );
  }

  @override
  void dispose() {
    _orbitController.dispose();
    _fadeController.dispose();
    _emailController.dispose();
    _passwordController.dispose();
    super.dispose();
  }

  void _submitEmail(BuildContext context) {
    final email = _emailController.text.trim();
    final password = _passwordController.text;
    if (email.isEmpty || password.isEmpty) return;
    context.read<AuthViewModel>().signInWithEmail(email, password);
  }

  @override
  Widget build(BuildContext context) {
    return Consumer<AuthViewModel>(
      builder: (context, vm, _) {
        if (vm.state == ViewState.loading) {
          return const Scaffold(
            backgroundColor: Color(0xFF04040F),
            body: FullScreenLoader(message: 'Signing in…'),
          );
        }

        final screenHeight = MediaQuery.of(context).size.height;
        final bottomPadding = MediaQuery.of(context).viewPadding.bottom;

        return Scaffold(
          backgroundColor: const Color(0xFF04040F),
          resizeToAvoidBottomInset: false,
          body: Stack(
            fit: StackFit.expand,
            children: [
              // Layer 1: Animated black hole canvas
              AnimatedBuilder(
                animation: _orbitController,
                builder: (_, _) => CustomPaint(
                  painter: _BlackHolePainter(_orbitController.value),
                ),
              ),

              // Layer 2: Radial gradient overlay for contrast
              Container(
                decoration: BoxDecoration(
                  gradient: RadialGradient(
                    center: const Alignment(0.0, -0.25),
                    radius: 0.7,
                    colors: [
                      Colors.transparent,
                      const Color(0xFF04040F).withValues(alpha: 0.3),
                      const Color(0xFF04040F).withValues(alpha: 0.8),
                    ],
                    stops: const [0.0, 0.5, 1.0],
                  ),
                ),
              ),

              // Layer 3: Depth blur strip at the orbital horizon
              Positioned(
                top: screenHeight * 0.36,
                left: 0,
                right: 0,
                height: 80,
                child: BackdropFilter(
                  filter: ui.ImageFilter.blur(sigmaX: 5, sigmaY: 5),
                  child: Container(color: Colors.transparent),
                ),
              ),

              // Layer 4: Hero — icon, name, tagline (top 65%)
              Positioned(
                top: 0,
                left: 0,
                right: 0,
                height: screenHeight * 0.65,
                child: Column(
                  mainAxisAlignment: MainAxisAlignment.center,
                  children: [
                    FadeTransition(
                      opacity: _iconFade,
                      child: const Icon(
                        Icons.mic_none,
                        color: Color(0xFF7C6FCD),
                        size: 32,
                      ),
                    ),
                    const SizedBox(height: 8),
                    FadeTransition(
                      opacity: _nameFade,
                      child: Text(
                        'Aura',
                        style: GoogleFonts.cormorantGaramond(
                          fontSize: 72,
                          fontWeight: FontWeight.w300,
                          color: Colors.white,
                          letterSpacing: 3,
                        ),
                      ),
                    ),
                    const SizedBox(height: 6),
                    FadeTransition(
                      opacity: _taglineFade,
                      child: Text(
                        'AI that remembers you',
                        style: GoogleFonts.jetBrainsMono(
                          fontSize: 12,
                          fontWeight: FontWeight.w400,
                          letterSpacing: 2.0,
                          color: Colors.white.withValues(alpha: 0.5),
                        ),
                      ),
                    ),
                  ],
                ),
              ),

              // Layer 5: Auth buttons (bottom 35%)
              Positioned(
                top: screenHeight * 0.65,
                bottom: 0,
                left: 28,
                right: 28,
                child: FadeTransition(
                  opacity: _buttonsFade,
                  child: SingleChildScrollView(
                    physics: _showEmailForm
                        ? const ClampingScrollPhysics()
                        : const NeverScrollableScrollPhysics(),
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.stretch,
                      children: [
                        if (vm.error != null) ...[
                          ErrorDisplay(
                            error: vm.error!,
                            onDismiss: vm.clearError,
                          ),
                          const SizedBox(height: 12),
                        ],
                        _GoogleSignInButton(
                          onTap: () =>
                              context.read<AuthViewModel>().signInWithGoogle(),
                        ),
                        const SizedBox(height: 16),
                        Center(
                          child: Text(
                            'or',
                            style: TextStyle(
                              color: Colors.white.withValues(alpha: 0.28),
                              fontSize: 13,
                            ),
                          ),
                        ),
                        const SizedBox(height: 16),
                        if (!_showEmailForm) ...[
                          _EmailContinueButton(
                            onTap: () =>
                                setState(() => _showEmailForm = true),
                          ),
                        ] else ...[
                          _DarkTextField(
                            controller: _emailController,
                            hint: 'Email address',
                            keyboardType: TextInputType.emailAddress,
                            textInputAction: TextInputAction.next,
                          ),
                          const SizedBox(height: 10),
                          _DarkTextField(
                            controller: _passwordController,
                            hint: 'Password',
                            obscureText: _obscurePassword,
                            textInputAction: TextInputAction.done,
                            onSubmitted: (_) => _submitEmail(context),
                            suffixIcon: GestureDetector(
                              onTap: () => setState(
                                  () => _obscurePassword = !_obscurePassword),
                              child: Icon(
                                _obscurePassword
                                    ? Icons.visibility_off_outlined
                                    : Icons.visibility_outlined,
                                color: Colors.white38,
                                size: 20,
                              ),
                            ),
                          ),
                          const SizedBox(height: 14),
                          _EmailContinueButton(
                            onTap: () => _submitEmail(context),
                            label: 'Sign in',
                            icon: Icons.arrow_forward,
                          ),
                        ],
                        const SizedBox(height: 20),
                        _LegalFooter(),
                        SizedBox(height: bottomPadding + 16),
                      ],
                    ),
                  ),
                ),
              ),
            ],
          ),
        );
      },
    );
  }
}

// Google sign-in button

class _GoogleSignInButton extends StatelessWidget {
  final VoidCallback onTap;
  const _GoogleSignInButton({required this.onTap});

  @override
  Widget build(BuildContext context) {
    return GestureDetector(
      onTap: onTap,
      child: Container(
        height: 52,
        decoration: BoxDecoration(
          color: Colors.white.withValues(alpha: 0.04),
          borderRadius: BorderRadius.circular(14),
          border: Border.all(
            color: Colors.white.withValues(alpha: 0.15),
          ),
        ),
        child: Row(
          mainAxisAlignment: MainAxisAlignment.center,
          children: const [
            Text(
              'G',
              style: TextStyle(
                color: Color(0xFF4285F4),
                fontSize: 18,
                fontWeight: FontWeight.w700,
              ),
            ),
            SizedBox(width: 12),
            Text(
              'Continue with Google',
              style: TextStyle(
                color: Colors.white,
                fontSize: 15,
                fontWeight: FontWeight.w400,
              ),
            ),
          ],
        ),
      ),
    );
  }
}

// Email button

class _EmailContinueButton extends StatelessWidget {
  final VoidCallback onTap;
  final String label;
  final IconData icon;

  const _EmailContinueButton({
    required this.onTap,
    this.label = 'Continue with Email',
    this.icon = Icons.email_outlined,
  });

  @override
  Widget build(BuildContext context) {
    return GestureDetector(
      onTap: onTap,
      child: Container(
        height: 52,
        decoration: BoxDecoration(
          color: const Color(0xFF5C6BC0).withValues(alpha: 0.85),
          borderRadius: BorderRadius.circular(14),
        ),
        child: Row(
          mainAxisAlignment: MainAxisAlignment.center,
          children: [
            Icon(icon, color: Colors.white, size: 18),
            const SizedBox(width: 10),
            Text(
              label,
              style: const TextStyle(
                color: Colors.white,
                fontSize: 15,
                fontWeight: FontWeight.w400,
              ),
            ),
          ],
        ),
      ),
    );
  }
}

// Text field

class _DarkTextField extends StatelessWidget {
  final TextEditingController controller;
  final String hint;
  final bool obscureText;
  final TextInputType? keyboardType;
  final TextInputAction? textInputAction;
  final ValueChanged<String>? onSubmitted;
  final Widget? suffixIcon;

  const _DarkTextField({
    required this.controller,
    required this.hint,
    this.obscureText = false,
    this.keyboardType,
    this.textInputAction,
    this.onSubmitted,
    this.suffixIcon,
  });

  @override
  Widget build(BuildContext context) {
    return Container(
      decoration: BoxDecoration(
        color: Colors.white.withValues(alpha: 0.06),
        borderRadius: BorderRadius.circular(14),
        border: Border.all(color: Colors.white.withValues(alpha: 0.12)),
      ),
      padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 4),
      child: TextField(
        controller: controller,
        obscureText: obscureText,
        keyboardType: keyboardType,
        textInputAction: textInputAction,
        onSubmitted: onSubmitted,
        style: const TextStyle(color: Colors.white, fontSize: 15),
        decoration: InputDecoration(
          hintText: hint,
          hintStyle: TextStyle(color: Colors.white.withValues(alpha: 0.35)),
          border: InputBorder.none,
          enabledBorder: InputBorder.none,
          focusedBorder: InputBorder.none,
          contentPadding: const EdgeInsets.symmetric(vertical: 10),
          isDense: true,
          suffixIcon: suffixIcon,
        ),
      ),
    );
  }
}

// Legal footer

class _LegalFooter extends StatelessWidget {
  static final _tosUri = Uri.parse('https://varuntej.dev/aura/terms-of-service');
  static final _privacyUri =
      Uri.parse('https://varuntej.dev/aura/privacy-policy');

  @override
  Widget build(BuildContext context) {
    return Text.rich(
      TextSpan(
        style: TextStyle(
          color: Colors.white.withValues(alpha: 0.28),
          fontSize: 11,
        ),
        children: [
          const TextSpan(text: 'By continuing you agree to our '),
          TextSpan(
            text: 'Terms of Service',
            style: TextStyle(
              color: Colors.white.withValues(alpha: 0.45),
              decoration: TextDecoration.underline,
            ),
            recognizer: TapGestureRecognizer()
              ..onTap = () =>
                  launchUrl(_tosUri, mode: LaunchMode.externalApplication),
          ),
          const TextSpan(text: '  ·  '),
          TextSpan(
            text: 'Privacy Policy',
            style: TextStyle(
              color: Colors.white.withValues(alpha: 0.45),
              decoration: TextDecoration.underline,
            ),
            recognizer: TapGestureRecognizer()
              ..onTap = () =>
                  launchUrl(_privacyUri, mode: LaunchMode.externalApplication),
          ),
        ],
      ),
      textAlign: TextAlign.center,
    );
  }
}
