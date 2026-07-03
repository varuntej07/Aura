import 'package:flutter/material.dart';

import '../../../core/theme/desktop_glass_theme.dart';

/// A single keyboard key drawn as a clear-glass chip: a near-invisible fill,
/// just enough to catch light, and a thin bright hairline edge — not a
/// physical raised key. The heavier gradient + drop-shadow read as a solid
/// button rather than glass once the surface underneath it is genuinely
/// see-through (2026-07-09 pass).
class KeyCap extends StatelessWidget {
  const KeyCap({super.key, required this.label});

  final String label;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 7, vertical: 3),
      decoration: BoxDecoration(
        color: DesktopGlassColors.fieldFill,
        borderRadius: BorderRadius.circular(10),
        border: Border.all(color: DesktopGlassColors.borderTop),
      ),
      child: Text(
        label,
        style: const TextStyle(
          fontFamily: 'GeistMono',
          fontSize: 10,
          fontWeight: FontWeight.w500,
          height: 1.2,
          color: DesktopGlassColors.textBright,
        ),
      ),
    );
  }
}

/// A keycap combo plus what it does, e.g. [Ctrl][Alt][B] toggle Buddy.
class HotkeyHint extends StatelessWidget {
  const HotkeyHint({super.key, required this.keys, required this.action});

  final List<String> keys;
  final String action;

  @override
  Widget build(BuildContext context) {
    return Row(
      mainAxisSize: MainAxisSize.min,
      children: [
        for (var i = 0; i < keys.length; i++) ...[
          if (i > 0) const SizedBox(width: 3),
          KeyCap(label: keys[i]),
        ],
        const SizedBox(width: 6),
        Text(
          action,
          style: const TextStyle(
            fontSize: 11,
            color: DesktopGlassColors.textDim,
            shadows: DesktopGlassColors.textShadows,
          ),
        ),
      ],
    );
  }
}
