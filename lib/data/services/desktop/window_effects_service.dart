import 'package:flutter/services.dart';

import '../../../core/logging/app_logger.dart';
import 'desktop_crash_log.dart';

const _tag = 'WindowEffects';

/// Dart side of the "aura/window_effects" method channel implemented in
/// windows/runner/window_effects_channel.cpp. The overlay window is
/// permanently borderless and fully transparent (no OS backdrop, no OS
/// corner rounding) — [ensureTransparent] just reasserts that native state on
/// every presentation change, matching the call pattern that already
/// reliably worked. All visible "glass" (fill, border, rounded corners at any
/// radius) is painted by Flutter itself (`_GlassSurface` in overlay_panel.dart),
/// not by the OS, since native blur+rounding never reliably rendered as one
/// consistent shape (see window_effects_channel.cpp for the full story).
class WindowEffectsService {
  static const _channel = MethodChannel('aura/window_effects');

  Future<void> ensureTransparent() async {
    try {
      await _channel.invokeMethod<bool>('ensureTransparent');
    } catch (e, st) {
      AppLogger.warning('Window effects call failed',
          tag: _tag, metadata: {'error': e.toString()});
      DesktopCrashLog.record(_tag, e, st);
    }
  }

  /// Forces OS keyboard focus onto the overlay window after show. Implemented
  /// natively (AttachThreadInput handshake) because the plain
  /// SetForegroundWindow that window_manager's focus() issues is denied while
  /// another process owns the foreground — exactly the hotkey-summon case.
  /// Returns whether the window actually became the foreground window.
  Future<bool> forceFocus() async {
    try {
      return await _channel.invokeMethod<bool>('focusWindow') ?? false;
    } catch (e, st) {
      AppLogger.warning('Native focus call failed',
          tag: _tag, metadata: {'error': e.toString()});
      DesktopCrashLog.record(_tag, e, st);
      return false;
    }
  }
}
