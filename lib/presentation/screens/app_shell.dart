import 'dart:ui';

import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';

import '../../core/theme/app_colors.dart';
import '../../core/theme/glass_card.dart';

/// Persistent shell rendered around the Home and Agents tabs.
/// Uses a floating glass pill nav bar over an ambient background.
class AppShell extends StatelessWidget {
  final Widget child;
  const AppShell({super.key, required this.child});

  static const _tabs = ['/home', '/agents'];

  int _tabIndex(BuildContext context) {
    final location = GoRouterState.of(context).matchedLocation;
    final i = _tabs.indexWhere((t) => location.startsWith(t));
    return i < 0 ? 0 : i;
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: AppColors.deepBackground,
      extendBody: true,
      body: AmbientBackground(child: child),
      bottomNavigationBar: _FloatingGlassNavBar(
        currentIndex: _tabIndex(context),
        onTap: (i) => context.go(_tabs[i]),
      ),
    );
  }
}

class _FloatingGlassNavBar extends StatelessWidget {
  final int currentIndex;
  final ValueChanged<int> onTap;

  const _FloatingGlassNavBar({
    required this.currentIndex,
    required this.onTap,
  });

  static const _items = [
    (
      icon: Icons.mic_none_rounded,
      activeIcon: Icons.mic_rounded,
      label: 'Home'
    ),
    (
      icon: Icons.extension_outlined,
      activeIcon: Icons.extension_rounded,
      label: 'Agents'
    ),
  ];

  @override
  Widget build(BuildContext context) {
    final bottomInset = MediaQuery.of(context).padding.bottom;
    return RepaintBoundary(
      child: Padding(
        padding: EdgeInsets.only(
          left: 52,
          right: 52,
          bottom: bottomInset + 16,
          top: 8,
        ),
        child: ClipRRect(
          borderRadius: BorderRadius.circular(32),
          child: BackdropFilter(
            filter: ImageFilter.blur(sigmaX: 20, sigmaY: 20),
            child: Container(
              height: 58,
              decoration: BoxDecoration(
                gradient: const LinearGradient(
                  begin: Alignment.topLeft,
                  end: Alignment.bottomRight,
                  colors: [AppColors.glassWhiteFill, AppColors.glassHighlight],
                ),
                borderRadius: BorderRadius.circular(32),
                border: Border.all(color: AppColors.glassBorderLight, width: 1),
                boxShadow: const [
                  BoxShadow(
                    color: Color(0x50000000),
                    blurRadius: 30,
                    offset: Offset(0, 10),
                  ),
                ],
              ),
              child: Row(
                mainAxisAlignment: MainAxisAlignment.spaceEvenly,
                children: List.generate(_items.length, (i) {
                  final item = _items[i];
                  final isActive = i == currentIndex;
                  return GestureDetector(
                    onTap: () => onTap(i),
                    behavior: HitTestBehavior.opaque,
                    child: SizedBox(
                      width: 88,
                      child: Column(
                        mainAxisAlignment: MainAxisAlignment.center,
                        children: [
                          AnimatedSwitcher(
                            duration: const Duration(milliseconds: 200),
                            child: Icon(
                              isActive ? item.activeIcon : item.icon,
                              key: ValueKey(isActive),
                              color: isActive
                                  ? AppColors.accent
                                  : AppColors.textTertiary,
                              size: 22,
                            ),
                          ),
                          const SizedBox(height: 3),
                          AnimatedDefaultTextStyle(
                            duration: const Duration(milliseconds: 200),
                            style: TextStyle(
                              color: isActive
                                  ? AppColors.accent
                                  : AppColors.textTertiary,
                              fontSize: 11,
                              fontWeight: isActive
                                  ? FontWeight.w600
                                  : FontWeight.w400,
                            ),
                            child: Text(item.label),
                          ),
                        ],
                      ),
                    ),
                  );
                }),
              ),
            ),
          ),
        ),
      ),
    );
  }
}
