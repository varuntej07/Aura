import 'package:launch_at_startup/launch_at_startup.dart';
import 'package:tray_manager/tray_manager.dart';

import '../../../core/logging/app_logger.dart';
import 'desktop_crash_log.dart';

/// System tray icon + menu: Open Buddy, Start with Windows toggle, Quit.
/// Left click summons the overlay; right click opens the menu.
class DesktopTrayService with TrayListener {
  DesktopTrayService({required this.onOpenBuddy, required this.onQuit});

  final void Function() onOpenBuddy;
  final Future<void> Function() onQuit;

  static const _menuKeyOpen = 'open_buddy';
  static const _menuKeyAutostart = 'start_with_windows';
  static const _menuKeyQuit = 'quit';

  Future<void> attach() async {
    trayManager.addListener(this);
    try {
      await trayManager.setIcon('assets/icons/tray_icon.ico');
      await trayManager.setToolTip('Buddy (Ctrl+Alt+B)');
      await _rebuildMenu();
    } catch (e, st) {
      AppLogger.error('Tray setup failed', error: e, tag: 'DesktopTray');
      DesktopCrashLog.record('DesktopTray', e, st);
    }
  }

  Future<void> _rebuildMenu() async {
    final autostartEnabled = await launchAtStartup.isEnabled();
    await trayManager.setContextMenu(Menu(items: [
      MenuItem(key: _menuKeyOpen, label: 'Open Buddy'),
      MenuItem.checkbox(
        key: _menuKeyAutostart,
        label: 'Start with Windows',
        checked: autostartEnabled,
      ),
      MenuItem.separator(),
      MenuItem(key: _menuKeyQuit, label: 'Quit Buddy'),
    ]));
  }

  @override
  void onTrayIconMouseDown() => onOpenBuddy();

  @override
  void onTrayIconRightMouseDown() => trayManager.popUpContextMenu();

  @override
  Future<void> onTrayMenuItemClick(MenuItem menuItem) async {
    switch (menuItem.key) {
      case _menuKeyOpen:
        onOpenBuddy();
      case _menuKeyAutostart:
        if (await launchAtStartup.isEnabled()) {
          await launchAtStartup.disable();
        } else {
          await launchAtStartup.enable();
        }
        await _rebuildMenu();
      case _menuKeyQuit:
        await onQuit();
    }
  }
}
