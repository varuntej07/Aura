import 'dart:async';

import 'package:flutter/material.dart';
import 'package:window_manager/window_manager.dart';

import '../../../core/logging/app_logger.dart';
import 'desktop_screen_capture_service.dart';
import 'overlay_controller.dart';

const _tag = 'PointingOverlay';

/// How long the pointer owns the screen: fly in, hold with the label bubble,
/// fade. Kept short because the window is click-through for the duration.
const pointingAnimationDuration = Duration(milliseconds: 3400);

/// Maps a point in a frame's JPEG pixel space to the shared logical coordinate
/// space `screen_retriever` and `window_manager` position windows in. Pure and
/// unit-tested: JPEG px -> monitor-relative physical px (clamped) -> global
/// physical px -> divided by the monitor's own scale factor.
Offset logicalPointFor({
  required ScreenFrameGeometry geometry,
  required int jpegX,
  required int jpegY,
}) {
  final clampedX = jpegX.clamp(0, geometry.jpegWidthPx).toDouble();
  final clampedY = jpegY.clamp(0, geometry.jpegHeightPx).toDouble();
  final physicalX = geometry.monitorLeftPx +
      clampedX * (geometry.monitorWidthPx / geometry.jpegWidthPx);
  final physicalY = geometry.monitorTopPx +
      clampedY * (geometry.monitorHeightPx / geometry.jpegHeightPx);
  return Offset(
    physicalX / geometry.scaleFactor,
    physicalY / geometry.scaleFactor,
  );
}

/// The captured monitor's bounds in the same logical space (what the pointing
/// window is sized to during a flight).
Rect logicalMonitorBoundsFor(ScreenFrameGeometry geometry) {
  return Rect.fromLTWH(
    geometry.monitorLeftPx / geometry.scaleFactor,
    geometry.monitorTopPx / geometry.scaleFactor,
    geometry.monitorWidthPx / geometry.scaleFactor,
    geometry.monitorHeightPx / geometry.scaleFactor,
  );
}

/// What the pointing surface renders: the target in WINDOW-LOCAL logical
/// coordinates (the window covers the whole target monitor) plus the label.
class ActivePointing {
  final Offset targetInWindow;
  final String label;

  const ActivePointing({required this.targetInWindow, required this.label});
}

/// Owns the window while Buddy points: takes it fullscreen click-through on
/// the frame's monitor, exposes the animation state the pointing surface
/// renders, and restores the previous presentation when the flight ends (or
/// is cancelled by hotkey/Esc/summon via [OverlayController.onCancelPointing]).
class PointingOverlayService extends ChangeNotifier {
  PointingOverlayService({required OverlayController overlayController})
      : _overlayController = overlayController {
    _overlayController.onCancelPointing = () => unawaited(cancel());
  }

  final OverlayController _overlayController;
  ActivePointing? _active;
  Timer? _restoreTimer;

  ActivePointing? get active => _active;

  /// Fly to `(jpegX, jpegY)` of the frame described by [geometry]. A new point
  /// arriving mid-flight replaces the current one (latest wins).
  Future<void> pointAt({
    required ScreenFrameGeometry geometry,
    required int jpegX,
    required int jpegY,
    required String label,
  }) async {
    _restoreTimer?.cancel();

    final monitorBounds = logicalMonitorBoundsFor(geometry);
    final target = logicalPointFor(
      geometry: geometry,
      jpegX: jpegX,
      jpegY: jpegY,
    );

    try {
      _overlayController.startPointing();
      await windowManager.setBounds(monitorBounds);
      // Click-through: every click during the flight lands in the app below,
      // so pointing never traps the user. Ctrl+Alt+B stays the escape hatch.
      await windowManager.setIgnoreMouseEvents(true);
      await windowManager.show();

      _active = ActivePointing(
        targetInWindow: target - monitorBounds.topLeft,
        label: label,
      );
      notifyListeners();
      AppLogger.info('Pointing at element', tag: _tag, metadata: {
        'label': label,
        'jpeg': '$jpegX,$jpegY',
        'logical': '${target.dx.round()},${target.dy.round()}',
      });

      _restoreTimer = Timer(pointingAnimationDuration, () => unawaited(cancel()));
    } catch (e, st) {
      AppLogger.error('Pointing failed, restoring overlay',
          error: e, stackTrace: st, tag: _tag);
      await cancel();
    }
  }

  /// Ends the flight (naturally or aborted) and hands the window back to the
  /// presentation the controller remembered.
  Future<void> cancel() async {
    _restoreTimer?.cancel();
    _restoreTimer = null;
    if (_active == null &&
        _overlayController.presentation != OverlayPresentation.pointing) {
      return;
    }
    _active = null;
    notifyListeners();
    try {
      // Interactivity first: the panel the controller restores must be clickable.
      await windowManager.setIgnoreMouseEvents(false);
    } catch (e) {
      AppLogger.warning('Failed to restore mouse events', tag: _tag,
          metadata: {'error': e.toString()});
    }
    _overlayController.endPointing();
  }

  @override
  void dispose() {
    _restoreTimer?.cancel();
    super.dispose();
  }
}
