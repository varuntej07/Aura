import 'dart:async';

import 'package:flutter/material.dart';

import '../../core/theme/app_colors.dart';
import '../../core/theme/glass_card.dart';

/// Shows a single tappable glass pill floating at the centre of the screen,
/// prompting a logged-out (guest) user to sign in. The whole pill is the
/// action: tapping it runs [onSignIn]. It fades in, auto-dismisses after a few
/// seconds, and is non-blocking (taps outside it pass through to the screen
/// below). Uses an Overlay rather than a SnackBar so it sits centre-screen,
/// in thumb reach, instead of pinned to the bottom edge. The pill itself is the
/// shared [FauxGlassCard.pill] so it reads like every other glass pill.
void showSignInPill(
  BuildContext context, {
  required String label,
  required VoidCallback onSignIn,
}) {
  final overlay = Overlay.of(context);

  late final OverlayEntry entry;
  entry = OverlayEntry(
    builder: (context) => _SignInPill(
      label: label,
      onSignIn: onSignIn,
      onDismiss: () => entry.remove(),
    ),
  );

  overlay.insert(entry);
}

class _SignInPill extends StatefulWidget {
  final String label;
  final VoidCallback onSignIn;
  final VoidCallback onDismiss;

  const _SignInPill({
    required this.label,
    required this.onSignIn,
    required this.onDismiss,
  });

  @override
  State<_SignInPill> createState() => _SignInPillState();
}

class _SignInPillState extends State<_SignInPill>
    with SingleTickerProviderStateMixin {
  late final AnimationController _controller;
  late final Animation<double> _fadeAnimation;
  Timer? _dismissTimer;

  @override
  void initState() {
    super.initState();
    _controller = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 200),
    );
    _fadeAnimation = CurvedAnimation(parent: _controller, curve: Curves.easeOut);
    _controller.forward();

    // Long enough to read and reach for; re-tapping the orb re-shows it.
    _dismissTimer = Timer(const Duration(seconds: 5), _dismiss);
  }

  void _dismiss() {
    _dismissTimer?.cancel();
    _controller.reverse().then((_) {
      if (mounted) widget.onDismiss();
    });
  }

  @override
  void dispose() {
    _dismissTimer?.cancel();
    _controller.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    // A bare Center over the overlay: only the pill is hit-testable, so taps
    // elsewhere fall through to the voice panel below.
    return Center(
      child: FadeTransition(
        opacity: _fadeAnimation,
        child: Material(
          color: Colors.transparent,
          child: GestureDetector(
            behavior: HitTestBehavior.opaque,
            onTap: () {
              _dismiss();
              widget.onSignIn();
            },
            child: FauxGlassCard.pill(
              child: Row(
                mainAxisSize: MainAxisSize.min,
                children: [
                  Text(
                    widget.label,
                    style: const TextStyle(
                      color: AppColors.textPrimary,
                      fontSize: 15,
                      fontWeight: FontWeight.w600,
                    ),
                  ),
                  const SizedBox(width: 8),
                  const Icon(
                    Icons.arrow_outward_rounded,
                    size: 16,
                    color: AppColors.textSecondary,
                  ),
                ],
              ),
            ),
          ),
        ),
      ),
    );
  }
}
