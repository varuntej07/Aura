import 'dart:async';

import 'package:firebase_crashlytics/firebase_crashlytics.dart';
import 'package:flutter/foundation.dart';
import 'package:posthog_flutter/posthog_flutter.dart';
import '../config/firebase_runtime.dart';

enum LogLevel { debug, info, warning, error, network }

class AppLogger {
  AppLogger._();

  static final RegExp _emailPattern = RegExp(
    r'\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b',
    caseSensitive: false,
  );
  static final RegExp _bearerPattern = RegExp(
    r'\bBearer\s+[A-Za-z0-9._~+/=-]+',
    caseSensitive: false,
  );
  static const Set<String> _safeMetadataKeys = {
    'code',
    'error_type',
    'operation',
    'provider',
    'reason',
    'status',
  };

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
      LogLevel.debug => 'DEBUG',
      LogLevel.info => 'INFO ',
      LogLevel.warning => 'WARN ',
      LogLevel.error => 'ERROR',
      LogLevel.network => 'NET  ',
    };
    final metaStr = metadata != null && metadata.isNotEmpty
        ? '  | ${metadata.entries.map((e) => '${e.key}=${e.value}').join(', ')}'
        : '';

    debugPrint('$timestamp  $levelStr  $tagStr$message$metaStr');
    if (level == LogLevel.warning || level == LogLevel.error) {
      _captureRemoteLog(level, message, tag: tag, metadata: metadata);
    }
  }

  static void _captureRemoteLog(
    LogLevel level,
    String message, {
    String? tag,
    Map<String, dynamic>? metadata,
  }) {
    if (kDebugMode) return;
    var safeMessage = message
        .replaceAll(_emailPattern, '<redacted-email>')
        .replaceAll(_bearerPattern, 'Bearer <redacted>');
    if (safeMessage.length > 240) safeMessage = safeMessage.substring(0, 240);
    final properties = <String, Object>{
      'severity': level == LogLevel.error ? 'ERROR' : 'WARNING',
      'message': safeMessage,
      'tag': tag ?? 'app',
      'platform': defaultTargetPlatform.name,
    };
    for (final entry in (metadata ?? const <String, dynamic>{}).entries) {
      if (!_safeMetadataKeys.contains(entry.key)) continue;
      final value = entry.value;
      if (value is String || value is num || value is bool) {
        properties[entry.key] = value as Object;
      }
    }
    unawaited(
      Posthog()
          .capture(eventName: 'client_log', properties: properties)
          .catchError((Object _) {}),
    );
  }
}
