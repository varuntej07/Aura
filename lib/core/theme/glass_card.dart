import 'dart:ui';

import 'package:flutter/material.dart';

import 'app_colors.dart';

// GlassCard
// Real BackdropFilter blur, RepaintBoundary-isolated.
// Use ONLY on static / rarely-rebuilt elements: nav bar, login form, paywall cards,
// settings panels. Never inside ListView, GridView, or AnimatedBuilder.
class GlassCard extends StatelessWidget {
  final Widget child;
  final double borderRadius;
  final EdgeInsetsGeometry? padding;
  final EdgeInsetsGeometry? margin;
  final double blurSigma;
  final Color? borderColor;
  final List<BoxShadow>? shadows;

  const GlassCard({
    super.key,
    required this.child,
    this.borderRadius = 20,
    this.padding,
    this.margin,
    this.blurSigma = 12,
    this.borderColor,
    this.shadows,
  });

  @override
  Widget build(BuildContext context) {
    return RepaintBoundary(
      child: Container(
        margin: margin,
        child: ClipRRect(
          borderRadius: BorderRadius.circular(borderRadius),
          child: BackdropFilter(
            filter: ImageFilter.blur(sigmaX: blurSigma, sigmaY: blurSigma),
            child: Container(
              padding: padding,
              decoration: BoxDecoration(
                gradient: const LinearGradient(
                  begin: Alignment.topLeft,
                  end: Alignment.bottomRight,
                  colors: [AppColors.glassWhiteFill, AppColors.glassHighlight],
                ),
                borderRadius: BorderRadius.circular(borderRadius),
                border: Border.all(
                  color: borderColor ?? AppColors.glassBorderLight,
                  width: 1,
                ),
                boxShadow: shadows ??
                    const [
                      BoxShadow(
                        color: Color(0x40000000),
                        blurRadius: 24,
                        offset: Offset(0, 6),
                      ),
                    ],
              ),
              child: child,
            ),
          ),
        ),
      ),
    );
  }
}

// FauxGlassCard
// Gradient + shimmer border, NO backdrop blur.
// Use in all scrolling lists, animated containers, message bubbles, suggestion
// pills — anywhere BackdropFilter would hurt scroll performance.
//
// Prefer named constructors for standard roles:
//   .pill — suggestion/tag chips
//   .navTile — navigation and info display tiles
//   .section — panel/section containers
//   .toggleTile — switch/toggle wrappers
//   .destructiveButton — sign-out / delete-account buttons
class FauxGlassCard extends StatelessWidget {
  final Widget child;
  final double borderRadius;
  final EdgeInsetsGeometry? padding;
  final EdgeInsetsGeometry? margin;
  final Color? borderColor;
  final Gradient? gradient;

  const FauxGlassCard({
    super.key,
    required this.child,
    this.borderRadius = 16,
    this.padding,
    this.margin,
    this.borderColor,
    this.gradient,
  });

  const FauxGlassCard.pill({
    super.key,
    required this.child,
  })  : borderRadius = 20,
        padding = const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
        margin = null,
        borderColor = null,
        gradient = null;

  const FauxGlassCard.navTile({
    super.key,
    required this.child,
  })  : borderRadius = 16,
        padding = const EdgeInsets.symmetric(horizontal: 16, vertical: 14),
        margin = null,
        borderColor = null,
        gradient = null;

  const FauxGlassCard.section({
    super.key,
    required this.child,
  })  : borderRadius = 16,
        padding = const EdgeInsets.all(16),
        margin = null,
        borderColor = null,
        gradient = null;

  const FauxGlassCard.toggleTile({
    super.key,
    required this.child,
  })  : borderRadius = 16,
        padding = const EdgeInsets.symmetric(horizontal: 4),
        margin = null,
        borderColor = null,
        gradient = null;

  // borderColor = AppColors.error.withValues(alpha: 0.3) = 0x4DF06060
  // gradient colors = alpha 0.10 (0x1A) / 0.04 (0x0A)
  const FauxGlassCard.destructiveButton({
    super.key,
    required this.child,
  })  : borderRadius = 16,
        padding = const EdgeInsets.symmetric(vertical: 16),
        margin = null,
        borderColor = const Color(0x4DF06060),
        gradient = const LinearGradient(
          begin: Alignment.topLeft,
          end: Alignment.bottomRight,
          colors: [Color(0x1AF06060), Color(0x0AF06060)],
        );

  @override
  Widget build(BuildContext context) {
    return Container(
      margin: margin,
      padding: padding,
      decoration: BoxDecoration(
        gradient: gradient ??
            const LinearGradient(
              begin: Alignment.topLeft,
              end: Alignment.bottomRight,
              colors: [Color(0x14FFFFFF), Color(0x08FFFFFF)],
            ),
        borderRadius: BorderRadius.circular(borderRadius),
        border: Border.all(
          color: borderColor ?? AppColors.glassBorderDim,
          width: 1,
        ),
      ),
      child: child,
    );
  }
}

// GlassIconButton
// Circular glass button with backdrop blur. Isolated with RepaintBoundary.
class GlassIconButton extends StatelessWidget {
  final IconData icon;
  final VoidCallback onTap;
  final double size;
  final Color? iconColor;
  final double iconSize;

  const GlassIconButton({
    super.key,
    required this.icon,
    required this.onTap,
    this.size = 44,
    this.iconColor,
    this.iconSize = 20,
  });

  @override
  Widget build(BuildContext context) {
    return RepaintBoundary(
      child: GestureDetector(
        onTap: onTap,
        child: ClipOval(
          child: BackdropFilter(
            filter: ImageFilter.blur(sigmaX: 10, sigmaY: 10),
            child: Container(
              width: size,
              height: size,
              decoration: BoxDecoration(
                shape: BoxShape.circle,
                gradient: const LinearGradient(
                  begin: Alignment.topLeft,
                  end: Alignment.bottomRight,
                  colors: [AppColors.glassWhiteFill, AppColors.glassHighlight],
                ),
                border:
                    Border.all(color: AppColors.glassBorderLight, width: 1),
              ),
              child: Icon(
                icon,
                color: iconColor ?? AppColors.textPrimary,
                size: iconSize,
              ),
            ),
          ),
        ),
      ),
    );
  }
}

// AmbientBackground
// Static radial gradient orbs that give glass blur something to reveal.
// Wrap the root body of each screen with this.
class AmbientBackground extends StatelessWidget {
  final Widget child;

  const AmbientBackground({super.key, required this.child});

  @override
  Widget build(BuildContext context) {
    return Stack(
      fit: StackFit.expand,
      children: [
        const ColoredBox(color: AppColors.deepBackground),
        Positioned(
          top: -80,
          left: -80,
          child: IgnorePointer(
            child: Container(
              width: 360,
              height: 360,
              decoration: BoxDecoration(
                shape: BoxShape.circle,
                gradient: RadialGradient(
                  colors: [AppColors.glassOrb1, Colors.transparent],
                ),
              ),
            ),
          ),
        ),
        Positioned(
          bottom: -100,
          right: -60,
          child: IgnorePointer(
            child: Container(
              width: 300,
              height: 300,
              decoration: BoxDecoration(
                shape: BoxShape.circle,
                gradient: RadialGradient(
                  colors: [AppColors.glassOrb2, Colors.transparent],
                ),
              ),
            ),
          ),
        ),
        child,
      ],
    );
  }
}
