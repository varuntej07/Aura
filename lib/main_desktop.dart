import 'dart:async';
import 'dart:io' show Platform;

import 'package:flutter/material.dart';
import 'package:launch_at_startup/launch_at_startup.dart';
import 'package:provider/provider.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:tray_manager/tray_manager.dart';
import 'package:window_manager/window_manager.dart';
import 'package:windows_single_instance/windows_single_instance.dart';

import 'core/config/environment.dart';
import 'core/config/firebase_config.dart';
import 'core/errors/error_handler.dart';
import 'core/logging/app_logger.dart';
import 'data/services/desktop/desktop_crash_log.dart';
import 'data/services/desktop/desktop_hotkey_service.dart';
import 'data/services/desktop/desktop_tray_service.dart';
import 'data/services/desktop/desktop_window_service.dart';
import 'data/services/desktop/overlay_controller.dart';
import 'data/services/desktop/screen_sight_service.dart';
import 'data/services/desktop/window_effects_service.dart';
import 'di/desktop_providers.dart';
import 'presentation/screens/desktop/overlay_panel.dart';

/// Windows desktop entrypoint: the Buddy overlay (plan: distributed-humming-perlis).
/// Mobile entrypoint is main.dart and its boot sequence stays byte-identical;
/// the shared steps (Firebase init, ErrorHandler, prefs) are repeated here
/// deliberately because extracting them would force a reorder of the shipped
/// mobile boot path. Adopt a shared bootstrap at the next intentional mobile
/// boot change.
///
/// Run:   flutter run -d windows -t lib/main_desktop.dart
/// Build: flutter build windows -t lib/main_desktop.dart
void main(List<String> args) {
  final overlayController = OverlayController();
  // Created before DI (the hotkey wires at boot); attached to the voice stack
  // inside buildDesktopProviders. Toggling before attach only flips the flag.
  final screenSightService = ScreenSightService();
  bool firebaseReady = false;

  runZonedGuarded(
    () async {
      WidgetsFlutterBinding.ensureInitialized();

      // A second launch focuses the running overlay instead of duplicating the
      // tray icon and losing the hotkey to a dead registration (journey J2).
      await WindowsSingleInstance.ensureSingleInstance(
        args,
        'aura_buddy_desktop',
        onSecondWindow: (_) => overlayController.summon(),
      );

      await DesktopCrashLog.initialize();
      firebaseReady = await FirebaseConfig.initialize();
      ErrorHandler.init();
      ErrorHandler.setEnvironment(Environment.current.env.name);
      _chainCrashLogIntoFlutterErrors();

      final prefs = await SharedPreferences.getInstance();

      await windowManager.ensureInitialized();
      launchAtStartup.setup(
        appName: 'Buddy',
        appPath: Platform.resolvedExecutable,
      );

      const windowOptions = WindowOptions(
        size: overlaySetupPanelSize,
        backgroundColor: Colors.transparent,
        skipTaskbar: true,
        alwaysOnTop: true,
        titleBarStyle: TitleBarStyle.hidden,
      );

      final windowEffectsService = WindowEffectsService();
      final windowService = DesktopWindowService(
        controller: overlayController,
        windowEffects: windowEffectsService,
        prefs: prefs,
      );
      final hotkeyService = DesktopHotkeyService();
      final trayService = DesktopTrayService(
        onOpenBuddy: overlayController.summon,
        onQuit: () async {
          await hotkeyService.unregisterAll();
          await trayManager.destroy();
          await windowManager.destroy();
        },
      );

      windowManager.waitUntilReadyToShow(windowOptions, () async {
        await windowManager.setAsFrameless();
        await windowService.attach();
        await trayService.attach();
        final hotkeyRegistered =
            await hotkeyService.registerSummonHotkey(
                overlayController.hotkeyPressed);
        final screenSightHotkeyRegistered =
            await hotkeyService.registerScreenSightHotkey(
                screenSightService.toggleArmed);
        // First launch never boots silently to tray (journey J2): always land
        // on the panel. Hidden-boot arrives with autostart polish in M5.
        // Deferred past any in-flight build: notifying a provider-listened
        // controller mid-mount trips the framework's !_dirty assertion.
        WidgetsBinding.instance
            .addPostFrameCallback((_) => overlayController.summon());
        WidgetsBinding.instance.ensureVisualUpdate();
        AppLogger.info('Buddy desktop shell ready', tag: 'desktop', metadata: {
          'firebase_ready': firebaseReady,
          'hotkey_registered': hotkeyRegistered,
          'screen_sight_hotkey_registered': screenSightHotkeyRegistered,
        });
      });

      runApp(MultiProvider(
        providers: buildDesktopProviders(
          prefs,
          overlayController: overlayController,
          screenSightService: screenSightService,
          windowEffectsService: windowEffectsService,
        ),
        child: const DesktopOverlayApp(),
      ));
    },
    (error, stack) {
      AppLogger.error('Uncaught async error',
          error: error, stackTrace: stack, tag: 'desktop');
      DesktopCrashLog.record('zone', error, stack);
    },
  );
}

/// Chain the desktop file log behind whatever handler ErrorHandler installed;
/// never replaces it.
void _chainCrashLogIntoFlutterErrors() {
  final previousHandler = FlutterError.onError;
  FlutterError.onError = (details) {
    DesktopCrashLog.record('flutter', details.exception, details.stack);
    previousHandler?.call(details);
  };
}
