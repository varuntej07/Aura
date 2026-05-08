import 'package:flutter/material.dart';

class AppColors {
  AppColors._();

  // Backgrounds
  static const background = Color(0xFF0D0D0D);
  static const surface = Color(0xFF1A1A1A);
  static const surfaceVariant = Color(0xFF242424);
  static const cardBackground = Color(0xFF1E1E1E);

  // Text
  static const textPrimary = Color(0xFFFFFFFF);
  static const textSecondary = Color(0xFFB0B0B0);
  static const textTertiary = Color(0xFF6B6B6B);
  static const textDisabled = Color(0xFF3D3D3D);

  // Accent
  static const accent = Color(0xFF6C63FF);
  static const accentLight = Color(0xFF8B84FF);
  static const accentDark = Color(0xFF4A43CC);
  static const accentGlow = Color(0x336C63FF);

  // Status
  static const error = Color(0xFFFF4444);
  static const errorSurface = Color(0xFF2A1515);
  static const success = Color(0xFF44FF88);
  static const warning = Color(0xFFFFAA44);

  // Dividers / borders
  static const divider = Color(0xFF2A2A2A);
  static const border = Color(0xFF333333);

  // Glass morphism
  static const deepBackground = Color(0xFF080812);
  static const glassWhiteFill = Color(0x1FFFFFFF);
  static const glassBorderLight = Color(0x33FFFFFF);
  static const glassBorderDim = Color(0x1AFFFFFF);
  static const glassHighlight = Color(0x0DFFFFFF);
  static const glassOrb1 = Color(0x1A6C63FF);
  static const glassOrb2 = Color(0x15223880);

  // Mic states
  static const micIdle = accent;
  static const micListening = Color(0xFF44BBFF);
  static const micProcessing = Color(0xFFFFAA44);
  static const micGlow = Color(0x446C63FF);
}
