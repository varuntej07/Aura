import 'package:flutter/material.dart';

import '../../core/theme/app_colors.dart';
import '../../core/theme/glass_card.dart';

/// Persistent shell rendered around the single Home surface.
/// Provides the ambient background that screen content blurs over. There is no
/// bottom navigation. Home is the only tab.
class AppShell extends StatelessWidget {
  final Widget child;
  const AppShell({super.key, required this.child});

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: AppColors.deepBackground,
      body: AmbientBackground(child: child),
    );
  }
}
