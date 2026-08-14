import 'dart:convert';

import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';

import '../../core/errors/error_handler.dart';
import '../../core/logging/app_logger.dart';

/// Dart half of the native startup breadcrumb.
///
/// The native side ([StartupTrace] / [StartupForensics] in Kotlin) can only see
/// as far as the Flutter engine. Everything after that — Firebase init, PostHog
/// setup, SharedPreferences, the first frame — happens in Dart, and those are
/// precisely the steps that can hang or throw before `runApp` is ever called.
/// Stamping them onto the same on-disk breadcrumb means a launch that dies
/// anywhere in `main()` still names the step it died in, on the NEXT launch.
///
/// Every method is best-effort and never throws. A diagnostic that can break
/// startup would defeat its own purpose, and this runs inside the startup path
/// it is measuring. On iOS and every non-Android platform the channel simply
/// isn't registered, so all of this quietly no-ops.
class StartupDiagnosticsService {
  StartupDiagnosticsService._();

  static const MethodChannel _channel = MethodChannel(
    'dev.varuntej.aura/diagnostics',
  );

  // Stage names. These strings are the contract with StartupTrace.kt — keep the
  // two in sync. Ordered by when they occur in startup.
  static const String stageDartMain = 'dart_main';
  static const String stageFirebaseReady = 'firebase_ready';
  static const String stagePostHogReady = 'posthog_ready';
  static const String stageBridgesReady = 'bridges_ready';
  static const String stagePrefsReady = 'prefs_ready';
  static const String stageRunApp = 'run_app';

  static const String _tag = 'StartupDiagnostics';

  /// Records that startup reached [stage].
  static Future<void> stamp(String stage) async {
    try {
      await _channel.invokeMethod<bool>('stampStage', {'stage': stage});
    } catch (_) {
      // Non-Android, channel missing, or platform thread busy. Never fatal.
    }
  }

  /// Clears the breadcrumb. Called only from the first rendered frame, which is
  /// the single unambiguous definition of a launch that actually worked.
  static Future<void> markLaunchSucceeded() async {
    try {
      await _channel.invokeMethod<bool>('markLaunchSucceeded');
    } catch (_) {}
  }

  /// Collects the previous launch's forensics, if that launch died before
  /// rendering, and files it as a Crashlytics non-fatal.
  ///
  /// The native beacon has already sent this report over raw HTTP by the time
  /// this runs — that path is the one that works when the app dies immediately.
  /// This second path exists because if the app DOES survive this launch, the
  /// record belongs in Crashlytics next to everything else, with the exit reason
  /// and boot stage promoted to searchable custom keys.
  static Future<void> reportPreviousLaunchFailure() async {
    try {
      final raw = await _channel.invokeMethod<String>('consumeStartupForensics');
      if (raw == null || raw.isEmpty) return;

      final report = jsonDecode(raw) as Map<String, dynamic>;
      final previousStage = report['previous_stage'] as String?;
      final consecutiveFailures =
          (report['consecutive_failed_launches'] as num?)?.toInt() ?? 0;
      final exits = report['exits'] as List<dynamic>? ?? const [];
      final latestExit = exits.isNotEmpty
          ? exits.first as Map<String, dynamic>
          : const <String, dynamic>{};

      AppLogger.warning(
        'Previous launch died before first frame',
        tag: _tag,
        metadata: {
          'stage': previousStage ?? 'unknown',
          'reason': latestExit['reason'] ?? 'unknown',
          'consecutive': consecutiveFailures,
        },
      );

      // Custom keys rather than a blob, so these are filterable in the
      // Crashlytics console. The stage answers "where" and the exit reason
      // answers "why" — the two facts this whole mechanism exists to obtain.
      ErrorHandler.setCustomKeys({
        'startup_previous_stage': previousStage ?? 'unknown',
        'startup_exit_reason': latestExit['reason']?.toString() ?? 'unknown',
        'startup_exit_description':
            latestExit['description']?.toString() ?? 'none',
        'startup_exit_status': latestExit['status']?.toString() ?? 'none',
        'startup_consecutive_failures': consecutiveFailures,
      });

      ErrorHandler.handle(
        StartupFailureReport(
          previousStage: previousStage,
          exitReason: latestExit['reason']?.toString(),
          consecutiveFailures: consecutiveFailures,
          rawReport: raw,
        ),
        StackTrace.current,
      );
    } catch (error, stack) {
      // Reporting a failure must not itself become one.
      if (kDebugMode) {
        AppLogger.warning(
          'Could not read startup forensics: $error',
          tag: _tag,
        );
      }
      debugPrint('$stack');
    }
  }
}

/// A synthesized error representing "the previous launch never rendered".
///
/// Not a real thrown exception — it exists so the forensics land in Crashlytics
/// as a grouped, searchable non-fatal rather than a loose log line. [toString]
/// is what Crashlytics uses for issue titles, so it leads with the stage and
/// reason to keep distinct failure modes in distinct issues.
class StartupFailureReport implements Exception {
  final String? previousStage;
  final String? exitReason;
  final int consecutiveFailures;
  final String rawReport;

  const StartupFailureReport({
    required this.previousStage,
    required this.exitReason,
    required this.consecutiveFailures,
    required this.rawReport,
  });

  @override
  String toString() =>
      'StartupFailureReport(stage: ${previousStage ?? 'unknown'}, '
      'reason: ${exitReason ?? 'unknown'}, '
      'consecutive: $consecutiveFailures)';
}
