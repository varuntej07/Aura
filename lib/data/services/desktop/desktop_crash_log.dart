import 'dart:io';

import 'package:flutter/foundation.dart';

/// Minimal desktop crash visibility. Crashlytics has no Windows plugin, so
/// uncaught errors append to a local rotating log the user can send us
/// (%LOCALAPPDATA%\Aura\logs\aura-desktop.log). Sentry is a captured TODO.
class DesktopCrashLog {
  DesktopCrashLog._();

  static const int _maxLogBytes = 1024 * 1024;
  static File? _logFile;

  static Future<void> initialize() async {
    try {
      final localAppData = Platform.environment['LOCALAPPDATA'];
      if (localAppData == null) return;
      final logsDir = Directory('$localAppData\\Aura\\logs');
      await logsDir.create(recursive: true);
      final file = File('${logsDir.path}\\aura-desktop.log');
      if (await file.exists() && await file.length() > _maxLogBytes) {
        final rotated = File('${logsDir.path}\\aura-desktop.1.log');
        if (await rotated.exists()) await rotated.delete();
        await file.rename(rotated.path);
      }
      _logFile = file;
    } catch (e) {
      debugPrint('[DesktopCrashLog] init failed: $e');
    }
  }

  /// Never throws: crash logging must not create crashes.
  static void record(String kind, Object error, StackTrace? stackTrace) {
    try {
      _logFile?.writeAsStringSync(
        '${DateTime.now().toIso8601String()} [$kind] $error\n'
        '${stackTrace ?? '(no stack)'}\n\n',
        mode: FileMode.append,
        flush: true,
      );
    } catch (_) {
      // Swallowed by design.
    }
  }
}
