import 'package:flutter/services.dart' show PhysicalKeyboardKey;
import 'package:hotkey_manager/hotkey_manager.dart';

import '../../../core/logging/app_logger.dart';

/// Registers the global hotkeys (hardcoded for v1; rebind UI is a captured
/// TODO): Ctrl+Alt+B summons/toggles the overlay, Ctrl+Alt+S arms/disarms
/// screen sight. Registration failure is LOUD, never silent: the caller
/// surfaces it and the tray menu / panel buttons remain the working paths.
///
/// Ordering contract: [registerSummonHotkey] clears ALL registrations first
/// (so a hot restart never double-registers), so it must be called before
/// [registerScreenSightHotkey].
class DesktopHotkeyService {
  Future<bool> registerSummonHotkey(void Function() onPressed) async {
    try {
      await hotKeyManager.unregisterAll();
      final summonHotkey = HotKey(
        key: PhysicalKeyboardKey.keyB,
        modifiers: [HotKeyModifier.control, HotKeyModifier.alt],
        scope: HotKeyScope.system,
      );
      await hotKeyManager.register(
        summonHotkey,
        keyDownHandler: (_) => onPressed(),
      );
      return true;
    } catch (e) {
      AppLogger.error(
        'Global hotkey registration failed. Another app may own Ctrl+Alt+B; '
        'tray menu remains the open path.',
        error: e,
        tag: 'DesktopHotkey',
      );
      return false;
    }
  }

  Future<bool> registerScreenSightHotkey(void Function() onPressed) async {
    try {
      final screenSightHotkey = HotKey(
        key: PhysicalKeyboardKey.keyS,
        modifiers: [HotKeyModifier.control, HotKeyModifier.alt],
        scope: HotKeyScope.system,
      );
      await hotKeyManager.register(
        screenSightHotkey,
        keyDownHandler: (_) => onPressed(),
      );
      return true;
    } catch (e) {
      AppLogger.error(
        'Screen sight hotkey registration failed. Another app may own '
        'Ctrl+Alt+S; the eye button on the panel remains the arm path.',
        error: e,
        tag: 'DesktopHotkey',
      );
      return false;
    }
  }

  Future<void> unregisterAll() => hotKeyManager.unregisterAll();
}
