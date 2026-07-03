import 'package:flutter/material.dart';

import 'app_colors.dart';

/// Dark glass design tokens for the Windows overlay ONLY. Mobile keeps the
/// warm cream system in app_colors.dart / app_theme.dart untouched; the
/// desktop overlay floats over arbitrary desktops, where a barely-there
/// frosted surface (Cluely-style HUD) reads as a tool overlay rather than an
/// app window.
abstract final class DesktopGlassColors {
  /// Primary fill for the overlay surface. Flutter paints the ENTIRE visible
  /// card now (2026-07-10) — no native OS blur/backdrop underneath it at all
  /// (see window_effects_channel.cpp: native Acrylic/Mica + corner rounding
  /// never reliably rendered as one consistent shape, so the window is now
  /// always fully transparent and Flutter is the sole shape/fill authority,
  /// the same "gradient + border, no real blur" pattern as FauxGlassCard
  /// elsewhere in this app). At ~85% opacity so the card reads clearly
  /// against ANY desktop behind it — there's no blur left to soften
  /// high-frequency content there, so this needs real body, not a wash.
  static const surfaceFill = Color(0xD9101014);

  /// Faint top-edge lift, faded into [surfaceFill] within the first quarter
  /// of the surface. Kept deliberately subtle (a flat calm sheet, not a
  /// diagonal glossy-button shine) per the 2026-07-06 flat-glass pass.
  static const sheenFaint = Color(0x14FFFFFF);

  /// Hairline border: a calm, low-contrast top-to-bottom fade, not a bright
  /// catch-light stroke (that read as a glossy button edge, not glass).
  static const borderTop = Color(0x38FFFFFF);
  static const borderBottom = Color(0x16FFFFFF);

  /// Flat border for small inner elements (chips, keycaps, fields).
  static const border = Color(0x30FFFFFF);

  static const fieldFill = Color(0x14FFFFFF);
  static const textBright = Color(0xF7FFFFFF);
  static const textDim = Color(0xA8FFFFFF);

  /// Idle icon opacity, bumped up from the earlier ~54% so icons stay
  /// legible against a genuinely see-through surface instead of reading as
  /// a grey smudge (2026-07-08 icon-clarity pass).
  static const iconIdle = Color(0xC2FFFFFF);
  static const danger = Color(0xFFFF8A75);

  /// Soft drop shadow behind overlay text: keeps white type legible now that
  /// the glass is transparent enough for a bright desktop to sit right
  /// behind it.
  static const textShadows = [
    Shadow(color: Color(0x66000000), blurRadius: 8, offset: Offset(0, 1)),
  ];

  /// Overlay-scoped accent: [AppColors.accentBase] lifted toward white.
  /// Desktop only — the mobile brand teal stays exactly as documented in
  /// AppColors ("changing this one constant swaps the entire accent
  /// palette"); the HUD surface just needs a lighter read of the same hue
  /// against a dark, translucent background (2026-07-08 per user design
  /// direction: the saturated brand teal read heavy/dark on glass).
  static Color get accent =>
      Color.lerp(AppColors.accentBase, Colors.white, 0.38)!;

  /// Soft colored glow behind an active/armed icon (mic live, screen sight
  /// armed): the "clear + glowing" read the flat icon fill alone can't give.
  static Color get accentGlow => accent.withValues(alpha: 0.7);
}

/// Corner radius for every painted overlay surface. This is now the ONLY
/// rounding authority — the real window is permanently borderless and fully
/// transparent with no native rounding attempt of its own (windows/runner/
/// window_effects_channel.cpp), specifically so there is nothing outside
/// Flutter's own paint that could ever show through as a second, mismatched
/// shape ("two concentric squares", 2026-07-09/10: neither a manual
/// SetWindowRgn clip nor DWMWA_WINDOW_CORNER_PREFERENCE ever reliably
/// rounded the native window as one piece, since Windows offers no public
/// API for a custom radius past its own small system default anyway). Free
/// to set to whatever reads best — 60, per 2026-07-10 user design direction.
const double desktopGlassCornerRadius = 60;

/// Dark ThemeData for the overlay MaterialApp (desktop only). Same font
/// families as the mobile theme; dark scheme so form fields and buttons on
/// the sign-in/onboarding panel style themselves for the glass surface.
ThemeData buildDesktopGlassTheme() {
  final scheme = ColorScheme.fromSeed(
    seedColor: AppColors.accentBase,
    brightness: Brightness.dark,
  );
  const fieldRadius = BorderRadius.all(Radius.circular(20));
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
        shadows: DesktopGlassColors.textShadows,
      ),
      titleMedium: TextStyle(
        color: DesktopGlassColors.textBright,
        shadows: DesktopGlassColors.textShadows,
      ),
      bodyMedium: TextStyle(
        color: DesktopGlassColors.textBright,
        shadows: DesktopGlassColors.textShadows,
      ),
      bodySmall: TextStyle(
        color: DesktopGlassColors.textDim,
        shadows: DesktopGlassColors.textShadows,
      ),
    ),
    inputDecorationTheme: InputDecorationTheme(
      filled: true,
      fillColor: DesktopGlassColors.fieldFill,
      contentPadding:
          const EdgeInsets.symmetric(horizontal: 14, vertical: 10),
      labelStyle: const TextStyle(color: DesktopGlassColors.textDim),
      hintStyle: const TextStyle(color: DesktopGlassColors.iconIdle),
      // A brighter hairline than the general-purpose `border` constant: the
      // pairing code / email fields are the one interactive surface on the
      // setup sheet, and a crisp edge is what reads as "clear glass" rather
      // than "smudge" once the fill behind it is this light.
      enabledBorder: const OutlineInputBorder(
        borderRadius: fieldRadius,
        borderSide: BorderSide(color: DesktopGlassColors.borderTop),
      ),
      focusedBorder: OutlineInputBorder(
        borderRadius: fieldRadius,
        borderSide: BorderSide(color: scheme.primary, width: 1.2),
      ),
    ),
    filledButtonTheme: FilledButtonThemeData(
      style: FilledButton.styleFrom(
        padding: const EdgeInsets.symmetric(horizontal: 28, vertical: 12),
        shape: const StadiumBorder(),
        textStyle: const TextStyle(
          fontFamily: 'PlusJakartaSans',
          fontWeight: FontWeight.w600,
          fontSize: 14,
        ),
      ),
    ),
    textButtonTheme: TextButtonThemeData(
      style: TextButton.styleFrom(foregroundColor: scheme.primary),
    ),
    tooltipTheme: TooltipThemeData(
      decoration: BoxDecoration(
        color: const Color(0xEB1C1C22),
        borderRadius: BorderRadius.circular(12),
        border: Border.all(color: DesktopGlassColors.border),
      ),
      textStyle: const TextStyle(
        color: DesktopGlassColors.textBright,
        fontSize: 12,
      ),
    ),
  );
}
