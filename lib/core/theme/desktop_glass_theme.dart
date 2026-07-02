import 'package:flutter/material.dart';

import 'app_colors.dart';

/// Dark glass design tokens for the Windows overlay ONLY. Mobile keeps the
/// warm cream system in app_colors.dart / app_theme.dart untouched; the
/// desktop overlay floats over arbitrary desktops, where a dark frosted
/// surface (Cluely-style HUD) reads as a tool overlay rather than an app.
abstract final class DesktopGlassColors {
  /// Tint handed to the native acrylic call (windows/runner/
  /// window_effects_channel.cpp): charcoal at ~72% alpha, so the frosted
  /// desktop reads dark without going opaque.
  static const acrylicTint = Color(0xB817171B);

  /// Wash Flutter paints over the acrylic for depth; keeps content legible on
  /// a bright desktop while letting the frost show through.
  static const surfaceWash = Color(0x2E0E0E12);

  /// Near-opaque stand-in when the OS acrylic call is unavailable:
  /// translucent paint over an un-blurred desktop was unreadable in M1
  /// testing, so the fallback trades the frost for legibility.
  static const surfaceFallback = Color(0xF518181E);

  static const border = Color(0x24FFFFFF);
  static const chipFill = Color(0x14FFFFFF);
  static const textBright = Color(0xF2FFFFFF);
  static const textDim = Color(0x8AFFFFFF);
  static const iconIdle = Color(0x66FFFFFF);
  static const danger = Color(0xFFFF8A75);

  /// Active-state accent (armed screen sight, live mic): the one thread of
  /// brand continuity with the mobile app on the dark surface.
  static const accent = AppColors.accentBase;
}

/// Corner radius for every painted overlay surface. Matches the ~8px logical
/// rounding DWMWCP_ROUND applies to the real window silhouette, so the
/// painted edge and the actual window corner agree.
const double desktopGlassCornerRadius = 8;

/// Dark ThemeData for the overlay MaterialApp (desktop only). Same font
/// families as the mobile theme; dark scheme so form fields and buttons on
/// the sign-in/onboarding panel style themselves for the glass surface.
ThemeData buildDesktopGlassTheme() {
  final scheme = ColorScheme.fromSeed(
    seedColor: AppColors.accentBase,
    brightness: Brightness.dark,
  );
  return ThemeData(
    useMaterial3: true,
    brightness: Brightness.dark,
    colorScheme: scheme,
    scaffoldBackgroundColor: Colors.transparent,
    fontFamily: 'PlusJakartaSans',
    textTheme: const TextTheme(
      titleLarge: TextStyle(
        fontFamily: 'Outfit',
        fontWeight: FontWeight.w600,
        color: DesktopGlassColors.textBright,
      ),
      titleMedium: TextStyle(color: DesktopGlassColors.textBright),
      bodyMedium: TextStyle(color: DesktopGlassColors.textBright),
      bodySmall: TextStyle(color: DesktopGlassColors.textDim),
    ),
    inputDecorationTheme: InputDecorationTheme(
      labelStyle: const TextStyle(color: DesktopGlassColors.textDim),
      hintStyle: const TextStyle(color: DesktopGlassColors.iconIdle),
      enabledBorder: const UnderlineInputBorder(
        borderSide: BorderSide(color: DesktopGlassColors.border),
      ),
      focusedBorder: UnderlineInputBorder(
        borderSide: BorderSide(color: scheme.primary),
      ),
    ),
    tooltipTheme: TooltipThemeData(
      decoration: BoxDecoration(
        color: const Color(0xF224242B),
        borderRadius: BorderRadius.circular(6),
      ),
      textStyle: const TextStyle(
        color: DesktopGlassColors.textBright,
        fontSize: 12,
      ),
    ),
  );
}
