import 'package:flutter/material.dart';
import 'package:screen_retriever/screen_retriever.dart';
import 'package:window_manager/window_manager.dart';

import '../../../core/logging/app_logger.dart';
import '../../../core/theme/desktop_glass_theme.dart';
import 'overlay_controller.dart';
import 'window_effects_service.dart';

/// Tall sheet for sign-in/onboarding (forms need room).
const Size overlaySetupPanelSize = Size(560, 360);

/// Compact signed-in glass bar: sphere + one-line caption + action icons.
const Size overlayVoiceBarSize = Size(520, 64);

const Size overlayPillSize = Size(320, 56);
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
  DesktopWindowService({
    required OverlayController controller,
    required WindowEffectsService windowEffects,
  })  : _controller = controller,
        _windowEffects = windowEffects;

  final OverlayController _controller;
  final WindowEffectsService _windowEffects;
  OverlayPresentation? _appliedPresentation;
  OverlayPanelVariant? _appliedVariant;

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
    final variant = _controller.panelVariant;
    // Variant participates in the change check so a sign-in/sign-out while the
    // panel is on screen still resizes the window.
    if (target == _appliedPresentation && variant == _appliedVariant) return;
    _appliedPresentation = target;
    _appliedVariant = variant;
    switch (target) {
      case OverlayPresentation.hidden:
        await windowManager.hide();
      case OverlayPresentation.panel:
        // Glass before show, so the surface never flashes un-frosted.
        await _windowEffects.enableGlass(tint: DesktopGlassColors.acrylicTint);
        await _showSized(
          variant == OverlayPanelVariant.bar
              ? overlayVoiceBarSize
              : overlaySetupPanelSize,
          focus: true,
        );
      case OverlayPresentation.pill:
        await _windowEffects.enableGlass(tint: DesktopGlassColors.acrylicTint);
        // Pill appears because focus moved to another app; do not steal it back.
        await _showSized(overlayPillSize, focus: false);
      case OverlayPresentation.pointing:
        // The pointing service owns the window (fullscreen click-through) for
        // the flight's duration; touching bounds here would fight it. The
        // frost must drop though, or acrylic tints the entire monitor.
        await _windowEffects.disableGlass();
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
