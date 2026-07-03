import 'dart:async';

import 'package:flutter/material.dart';
import 'package:screen_retriever/screen_retriever.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:window_manager/window_manager.dart';

import '../../../core/logging/app_logger.dart';
import 'desktop_crash_log.dart';
import 'overlay_controller.dart';
import 'window_effects_service.dart';

/// Where a user-dragged overlay position is persisted, as its CENTER point
/// (not top-left) so resizing between presentations — setup sheet, voice bar,
/// pill, all different widths — keeps the window centered where the user put
/// it instead of drifting sideways. Two doubles, not one JSON blob: matches
/// the existing simple-key style already used elsewhere in this file.
const String _positionXKey = 'desktop_overlay_position_x';
const String _positionYKey = 'desktop_overlay_position_y';

/// Setup sheet width, fixed across every onboarding step (only height varies
/// per step, via [_setupHeightFor]). Also the boot-time default in
/// main_desktop.dart, before OverlayController resolves which step to show.
const Size overlaySetupPanelSize = Size(560, 360);

/// Per-step setup sheet heights: only the STARTING guess shown for a frame or
/// two before `_SetupPanel` measures its real content and reports it via
/// [OverlayController.reportMeasuredSetupHeight] — see
/// [DesktopWindowService._targetSetupHeight]. Don't need to be pixel-exact;
/// measurement corrects them immediately either way.
const double _setupHeightWelcome = 360;
const double _setupHeightGetApp = 300;
const double _setupHeightLink = 400;

double _setupHeightFor(DesktopOnboardingStep step) => switch (step) {
      DesktopOnboardingStep.welcome => _setupHeightWelcome,
      DesktopOnboardingStep.getApp => _setupHeightGetApp,
      DesktopOnboardingStep.link => _setupHeightLink,
    };

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
/// events back into it. The overlay appears top-center of the cursor's
/// display by default, UNTIL the user drags it (window_manager's
/// `DragToMoveArea`-style gesture in overlay_panel.dart) — from then on its
/// last dragged position (persisted to disk, keyed by center point) is the
/// new anchor for every future show/resize/relaunch, replacing the default.
class DesktopWindowService with WindowListener {
  DesktopWindowService({
    required OverlayController controller,
    required WindowEffectsService windowEffects,
    required SharedPreferences prefs,
  })  : _controller = controller,
        _windowEffects = windowEffects,
        _prefs = prefs;

  final OverlayController _controller;
  final WindowEffectsService _windowEffects;
  final SharedPreferences _prefs;
  OverlayPresentation? _appliedPresentation;
  OverlayPanelVariant? _appliedVariant;
  double? _appliedSetupHeight;
  Future<void> _applyChain = Future<void>.value();

  /// The user's last dragged position, as a center point — null until they
  /// drag it at least once (or a previously-saved position's monitor is no
  /// longer connected, see [_loadSavedPosition]), in which case every size
  /// falls back to the cursor-display-relative default via
  /// [overlayPositionFor].
  Offset? _userCenter;

  /// True only for the duration of OUR OWN `windowManager.setBounds()` calls.
  /// window_manager fires [onWindowMoved] for ANY bounds change, including
  /// ones we make ourselves (every presentation switch, every onboarding-step
  /// resize) — without this guard, the very first default-positioned show
  /// would immediately self-capture as if the user had dragged it there,
  /// permanently overriding the cursor-follows-display default before the
  /// user ever touched the window.
  bool _applyingBounds = false;

  /// [OverlayController.measuredSetupHeight] once `_SetupPanel` has measured
  /// its real content at least once; the per-step guess only for the first
  /// frame or two before that measurement lands.
  double _targetSetupHeight() =>
      _controller.measuredSetupHeight ??
      _setupHeightFor(_controller.onboardingStep);

  Future<void> attach() async {
    windowManager.addListener(this);
    await _loadSavedPosition();
    _controller.addListener(_enqueueApply);
    _enqueueApply();
    await _applyChain;
  }

  void detach() {
    windowManager.removeListener(this);
    _controller.removeListener(_enqueueApply);
  }

  /// Restores a position saved in an earlier session, but only if it still
  /// lands on a currently-connected display — monitor arrangements change
  /// (laptop undocked, a monitor unplugged), and opening off-screen with no
  /// way to drag it back would be worse than just re-centering.
  Future<void> _loadSavedPosition() async {
    final x = _prefs.getDouble(_positionXKey);
    final y = _prefs.getDouble(_positionYKey);
    if (x == null || y == null) return;
    final candidate = Offset(x, y);
    try {
      final displays = await screenRetriever.getAllDisplays();
      final onScreen = [for (final display in displays) _boundsOf(display)]
          .any((bounds) => bounds.contains(candidate));
      if (onScreen) {
        _userCenter = candidate;
      }
    } catch (e) {
      AppLogger.warning(
          'Display lookup failed while restoring overlay position',
          tag: 'DesktopWindow',
          metadata: {'error': e.toString()});
    }
  }

  /// The pointing service owns fullscreen bounds during a flight (unrelated
  /// to where the user wants the PANEL to live), so its moves are ignored.
  @override
  void onWindowMoved() {
    if (_applyingBounds) return;
    if (_controller.presentation == OverlayPresentation.pointing) return;
    unawaited(_captureUserPosition());
  }

  Future<void> _captureUserPosition() async {
    try {
      final bounds = await windowManager.getBounds();
      final center = bounds.center;
      _userCenter = center;
      await _prefs.setDouble(_positionXKey, center.dx);
      await _prefs.setDouble(_positionYKey, center.dy);
    } catch (e) {
      AppLogger.warning('Failed to persist overlay position',
          tag: 'DesktopWindow', metadata: {'error': e.toString()});
    }
  }

  /// The user's remembered center once they've dragged the overlay at least
  /// once, else the cursor-display-relative default.
  Future<Offset> _positionFor(Size size) async {
    final center = _userCenter;
    if (center != null) {
      return Offset(
          center.dx - size.width / 2, center.dy - size.height / 2);
    }
    final displayBounds = await _activeDisplayBounds();
    return overlayPositionFor(displayBounds: displayBounds, windowSize: size);
  }

  /// Window ops (hide/show/resize/focus) are async and must never interleave:
  /// two overlapping [_applyPresentation] runs can hide and show out of order,
  /// leaving the real window visible while the controller says hidden — the
  /// "hotkey needs several presses" desync. Every controller notification
  /// appends one run to a single chain; each run reads the CURRENT controller
  /// state when it executes, so a stale queued run collapses to a no-op.
  void _enqueueApply() {
    _applyChain = _applyChain
        .then((_) => _applyPresentation())
        .catchError((Object e, StackTrace st) {
      AppLogger.warning('Overlay presentation apply failed',
          tag: 'DesktopWindow', metadata: {'error': e.toString()});
      // AppLogger alone is invisible here: no attached console in a real
      // launch, and Crashlytics excludes Windows. Without this, a failure
      // leaves zero trace (2026-07-03).
      DesktopCrashLog.record('DesktopWindow', e, st);
    });
  }

  Future<void> _applyPresentation() async {
    final target = _controller.presentation;
    final variant = _controller.panelVariant;
    // Variant participates in the change check so a sign-in/sign-out while the
    // panel is on screen still resizes the window.
    if (target == _appliedPresentation && variant == _appliedVariant) {
      // Presentation/variant are unchanged, but the setup sheet's measured
      // target height may have changed (stepping through onboarding, an
      // error message appearing, anything) while the panel is already
      // visible and focused. Resize in place rather than rerunning the full
      // show/focus dance below, which would yank focus away from whatever
      // field the user is mid-typing in.
      if (target == OverlayPresentation.panel &&
          variant == OverlayPanelVariant.setup) {
        final height = _targetSetupHeight();
        if (height != _appliedSetupHeight) {
          await _resizeInPlace(Size(overlaySetupPanelSize.width, height));
          _appliedSetupHeight = height;
        }
      }
      return;
    }
    // Committed only after the operations below succeed (not before): if a
    // window op throws partway through, the cache must still reflect the last
    // KNOWN-APPLIED state, or the next trigger (hotkey/tray/second-instance)
    // sees target == _appliedPresentation and silently no-ops forever instead
    // of retrying. This exact desync froze the overlay from ever showing after
    // a first-boot failure (2026-07-03).
    switch (target) {
      case OverlayPresentation.hidden:
        await windowManager.hide();
      case OverlayPresentation.panel:
        // Reasserted before every show, defensively (see WindowEffectsService
        // doc comment) — the window is always transparent now, Flutter paints
        // the visible card.
        await _windowEffects.ensureTransparent();
        final isSetup = variant == OverlayPanelVariant.setup;
        final setupHeight = isSetup ? _targetSetupHeight() : null;
        await _showSized(
          isSetup
              ? Size(overlaySetupPanelSize.width, setupHeight!)
              : overlayVoiceBarSize,
          focus: true,
        );
        if (isSetup) _appliedSetupHeight = setupHeight;
      case OverlayPresentation.pill:
        await _windowEffects.ensureTransparent();
        // Pill appears because focus moved to another app; do not steal it back.
        await _showSized(overlayPillSize, focus: false);
      case OverlayPresentation.pointing:
        // The pointing service owns the window (fullscreen click-through) for
        // the flight's duration; touching bounds here would fight it.
        await _windowEffects.ensureTransparent();
    }
    _appliedPresentation = target;
    _appliedVariant = variant;
  }

  /// Resizes the already-visible, already-focused window to a new setup-sheet
  /// height without hiding/reshowing or refocusing it (unlike [_showSized]),
  /// so stepping through onboarding never interrupts whatever the user is
  /// doing on screen.
  Future<void> _resizeInPlace(Size size) async {
    final position = await _positionFor(size);
    _applyingBounds = true;
    try {
      await windowManager.setBounds(
        Rect.fromLTWH(position.dx, position.dy, size.width, size.height),
      );
    } finally {
      // Always cleared, even on failure — otherwise a single failed setBounds
      // call permanently disables position capture for the rest of the
      // session (every real drag would look like "our own move" forever).
      _applyingBounds = false;
    }
  }

  Future<void> _showSized(Size size, {required bool focus}) async {
    final position = await _positionFor(size);
    _applyingBounds = true;
    try {
      await windowManager.setBounds(
        Rect.fromLTWH(position.dx, position.dy, size.width, size.height),
      );
    } finally {
      _applyingBounds = false;
    }
    await windowManager.show();
    if (focus) {
      // window_manager's focus() is a bare SetForegroundWindow, which the OS
      // denies while another app owns the foreground (the hotkey-summon
      // case): the panel then sat visible but keyboard-dead — Esc went to the
      // other app and no blur event ever fired. The native call takes the
      // foreground reliably; one retry covers the show/animation race.
      var focused = await _windowEffects.forceFocus();
      if (!focused) {
        await Future<void>.delayed(const Duration(milliseconds: 60));
        focused = await _windowEffects.forceFocus();
      }
      if (!focused) {
        AppLogger.warning(
            'OS denied overlay focus; Esc is inactive until the panel is '
            'clicked',
            tag: 'DesktopWindow');
      }
    }
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
