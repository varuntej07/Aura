import 'package:flutter/material.dart';

/// Wraps a tappable card so a press is visible the instant a finger lands.
///
/// Without this, the glass tiles are bare [GestureDetector]s: nothing at all
/// happens until the next page finishes pushing, which reads as a laggy app even
/// when the frame budget is fine.
///
/// Deliberately not [InkWell]. A splash needs a [Material] ancestor and paints an
/// extra layer, and the ripple does not suit the glass surfaces. A short scale is
/// cheaper and matches the rest of the design.
class PressableTile extends StatefulWidget {
  final Widget child;

  /// Null disables the press state as well as the tap, so a dead tile does not
  /// animate as though it did something.
  final VoidCallback? onTap;

  /// How far to shrink while held. Subtle on purpose: this is feedback, not an
  /// animation anyone should notice.
  final double pressedScale;

  const PressableTile({
    super.key,
    required this.child,
    required this.onTap,
    this.pressedScale = 0.98,
  });

  @override
  State<PressableTile> createState() => _PressableTileState();
}

class _PressableTileState extends State<PressableTile> {
  bool _pressed = false;

  void _setPressed(bool value) {
    if (_pressed == value || widget.onTap == null) return;
    setState(() => _pressed = value);
  }

  @override
  Widget build(BuildContext context) {
    return GestureDetector(
      // Opaque so the whole card area is tappable, including its padding.
      behavior: HitTestBehavior.opaque,
      onTap: widget.onTap,
      onTapDown: (_) => _setPressed(true),
      onTapUp: (_) => _setPressed(false),
      onTapCancel: () => _setPressed(false),
      child: AnimatedScale(
        scale: _pressed ? widget.pressedScale : 1.0,
        duration: const Duration(milliseconds: 90),
        curve: Curves.easeOut,
        child: widget.child,
      ),
    );
  }
}
