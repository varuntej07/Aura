import 'package:firebase_crashlytics/firebase_crashlytics.dart';
import 'package:flutter/foundation.dart';
import '../config/firebase_runtime.dart';

enum LogLevel { debug, info, warning, error, network }

class AppLogger {
  AppLogger._();

  // Minimum level to print. In release builds you may raise this to warning.
  // For now, always print everything so issues are visible during development.
  static const LogLevel _minLevel = LogLevel.debug;

  static void debug(
    String message, {
    String? tag,
    Map<String, dynamic>? metadata,
  }) {
    _log(LogLevel.debug, message, tag: tag, metadata: metadata);
  }

  static void info(
    String message, {
    String? tag,
    Map<String, dynamic>? metadata,
  }) {
    _log(LogLevel.info, message, tag: tag, metadata: metadata);
  }

  static void warning(
    String message, {
    String? tag,
    Map<String, dynamic>? metadata,
  }) {
    _log(LogLevel.warning, message, tag: tag, metadata: metadata);
  }

  static void error(
    String message, {
    Object? error,
    StackTrace? stackTrace,
    String? tag,
    Map<String, dynamic>? metadata,
  }) {
    _log(LogLevel.error, message, tag: tag, metadata: metadata);
    if (error != null) debugPrint('  └─ Error: $error');
    if (stackTrace != null) debugPrint('  └─ Stack: $stackTrace');
    if (error != null && FirebaseRuntime.crashlyticsSupported) {
      FirebaseCrashlytics.instance.recordError(
        error,
        stackTrace,
        reason: message,
      );
    }
  }

  static void network(
    String method,
    String url,
    int statusCode,
    Duration latency,
  ) {
    _log(
      LogLevel.network,
      '$method $url → $statusCode (${latency.inMilliseconds}ms)',
      tag: 'Network',
    );
  }

  static void _log(
    LogLevel level,
    String message, {
    String? tag,
    Map<String, dynamic>? metadata,
  }) {
    if (level.index < _minLevel.index) return;

    final timestamp = DateTime.now().toIso8601String();
    final tagStr = tag != null ? '[$tag] ' : '';
    final levelStr = switch (level) {
      LogLevel.debug   => 'DEBUG',
      LogLevel.info    => 'INFO ',
      LogLevel.warning => 'WARN ',
      LogLevel.error   => 'ERROR',
      LogLevel.network => 'NET  ',
    };
    final metaStr = metadata != null && metadata.isNotEmpty
        ? '  | ${metadata.entries.map((e) => '${e.key}=${e.value}').join(', ')}'
        : '';

    debugPrint('$timestamp  $levelStr  $tagStr$message$metaStr');
  }
}
