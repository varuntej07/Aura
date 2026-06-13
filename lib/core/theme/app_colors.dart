import 'package:flutter/material.dart';

class AppColors {
  AppColors._();

  // Changing this one constant swaps the entire accent palette.
  // Teal:  Color(0xFF1EC8B0), Amber: Color(0xFFE8A020)
  static const accentBase = Color(0xFF1EC8B0);

  // All accent-derived colors computed from accentBase.
  // Accent is const so `const Icon(color: AppColors.accent)` still compiles.
  // Only accentLight/accentDark/accentGlow are getters, not const.
  static const accent = accentBase;
  static Color get accentLight => Color.lerp(accentBase, Colors.white, 0.20)!;
  static Color get accentDark => Color.lerp(accentBase, Colors.black, 0.34)!;
  static Color get accentGlow => accentBase.withValues(alpha: 0.18);
  static Color get glassOrb1 => accentBase.withValues(alpha: 0.10);
  static Color get glassOrb2 => accentBase.withValues(alpha: 0.06);
  static Color get micGlow => accentBase.withValues(alpha: 0.24);

  // Backgrounds: warm cream theme.
  static const background = Color(0xFFF4EEE2);
  static const surface = Color(0xFFFBF7EF);         // raised, near-white warm
  static const surfaceVariant = Color(0xFFEAE2D2);  // selected/active tint, darker than bg
  static const cardBackground = Color(0xFFFBF7EF);

  // Text: warm charcoal on cream
  static const textPrimary = Color(0xFF272622);
  static const textSecondary = Color(0xFF504D45);
  static const textTertiary = Color(0xFF79746A);
  static const textDisabled = Color(0xFFB8B2A6);

  // Status (tuned for contrast on light)
  static const error = Color(0xFFD64545);
  static const errorSurface = Color(0xFFF7E4E0);
  static const success = Color(0xFF1FA37A);
  static const warning = Color(0xFFD68A1F);

  // Dividers / borders: warm, low-contrast on cream
  static const divider = Color(0xFFE4DCCB);
  static const border = Color(0xFFDCD3C0);

  // Glass morphism: warm-tint tokens
  static const deepBackground = Color(0xFFF4EEE2);
  static const glassWhiteFill = Color(0x0A2B2A26);
  static const glassBorderLight = Color(0x1F2B2A26);
  static const glassBorderDim = Color(0x122B2A26);
  static const glassHighlight = Color(0x14FFFFFF);

  // Mic states
  static const micIdle = accentBase;
  static const micListening = Color(0xFF2E86C9);
  static const micProcessing = Color(0xFFD68A1F);
}
