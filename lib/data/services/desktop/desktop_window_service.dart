import 'package:flutter/material.dart';
import 'package:screen_retriever/screen_retriever.dart';
import 'package:window_manager/window_manager.dart';

import '../../../core/logging/app_logger.dart';
import 'overlay_controller.dart';

const Size overlayPanelSize = Size(560, 360);
const Size overlayPillSize = Size(320, 72);
const double overlayTopMargin = 48;

/// Pure positioning math, unit-tested without a real window.
Offset overlayPositionFor({
  required Rect displayBounds,
  required Size windowSize,
}) {
  return Offset(
    displayBounds.left + (displayBounds.width - windowSize.width) / 2,
    displayBounds.top + overlayTopMargin,
  );
}

/// Picks the display containing the cursor; falls back when the cursor sits on
/// coordinates no live display covers (disconnected monitor, failure mode #9).
Rect displayBoundsContaining({
  required Offset cursor,
  required List<Rect> displays,
  required Rect fallback,
}) {
  for (final bounds in displays) {
    if (bounds.contains(cursor)) return bounds;
  }
  return fallback;
}

/// Applies [OverlayController] state to the real window and feeds OS focus
/// events back into it. The overlay always appears top-center of the display
/// the cursor is on.
class DesktopWindowService with WindowListener {
  DesktopWindowService({required OverlayController controller})
      : _controller = controller;

  final OverlayController _controller;
  OverlayPresentation? _appliedPresentation;

  Future<void> attach() async {
    windowManager.addListener(this);
    _controller.addListener(_applyPresentation);
    await _applyPresentation();
  }

  void detach() {
    windowManager.removeListener(this);
    _controller.removeListener(_applyPresentation);
  }

  Future<void> _applyPresentation() async {
    final target = _controller.presentation;
    if (target == _appliedPresentation) return;
    _appliedPresentation = target;
    switch (target) {
      case OverlayPresentation.hidden:
        await windowManager.hide();
      case OverlayPresentation.panel:
        await _showSized(overlayPanelSize, focus: true);
      case OverlayPresentation.pill:
        // Pill appears because focus moved to another app; do not steal it back.
        await _showSized(overlayPillSize, focus: false);
      case OverlayPresentation.pointing:
        // The pointing service owns the window (fullscreen click-through) for
        // the flight's duration; touching bounds here would fight it.
        break;
    }
  }

  Future<void> _showSized(Size size, {required bool focus}) async {
    final displayBounds = await _activeDisplayBounds();
    final position =
        overlayPositionFor(displayBounds: displayBounds, windowSize: size);
    await windowManager.setBounds(
      Rect.fromLTWH(position.dx, position.dy, size.width, size.height),
    );
    await windowManager.show();
    if (focus) await windowManager.focus();
  }

  Future<Rect> _activeDisplayBounds() async {
    try {
      final cursor = await screenRetriever.getCursorScreenPoint();
      final displays = await screenRetriever.getAllDisplays();
      final primary = await screenRetriever.getPrimaryDisplay();
      return displayBoundsContaining(
        cursor: Offset(cursor.dx, cursor.dy),
        displays: [for (final display in displays) _boundsOf(display)],
        fallback: _boundsOf(primary),
      );
    } catch (e) {
      AppLogger.warning('Display lookup failed, using default bounds',
          tag: 'DesktopWindow', metadata: {'error': e.toString()});
      return const Rect.fromLTWH(0, 0, 1920, 1080);
    }
  }

  Rect _boundsOf(Display display) {
    final position = display.visiblePosition ?? Offset.zero;
    final size = display.visibleSize ?? display.size;
    return Rect.fromLTWH(position.dx, position.dy, size.width, size.height);
  }

  @override
  void onWindowBlur() => _controller.focusLost();
}
