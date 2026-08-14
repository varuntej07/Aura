import 'package:firebase_crashlytics/firebase_crashlytics.dart';
import 'package:flutter/foundation.dart';
import '../config/firebase_runtime.dart';
import '../logging/app_logger.dart';
import 'app_exception.dart';

class ErrorHandler {
  ErrorHandler._();

  static void init() {
    FlutterError.onError = (details) {
      AppLogger.error(
        'Flutter error',
        error: details.exception,
        stackTrace: details.stack,
        tag: 'ErrorHandler',
      );
      _withCrashlytics((crashlytics) {
        crashlytics.recordFlutterFatalError(details);
      });
    };

    PlatformDispatcher.instance.onError = (error, stack) {
      AppLogger.error(
        'Platform error',
        error: error,
        stackTrace: stack,
        tag: 'ErrorHandler',
      );
      _withCrashlytics((crashlytics) {
        crashlytics.recordError(error, stack, fatal: true);
      });
      return true;
    };
  }

  /// Records an uncaught error as FATAL. Used by the top-level zone handler.
  ///
  /// Routed through [_withCrashlytics] rather than touching
  /// `FirebaseCrashlytics.instance` directly: Crashlytics has no Windows/Linux
  /// plugin and the instance getter throws a synchronous assertion there, which
  /// would turn the crash reporter into a second crash. See [FirebaseRuntime].
  static void recordFatal(Object error, StackTrace stack) {
    if (kDebugMode) return;
    _withCrashlytics((crashlytics) {
      crashlytics.recordError(error, stack, fatal: true);
    });
  }

  static void handle(Object error, [StackTrace? stack]) {
    AppLogger.error(
      'Handled error',
      error: error,
      stackTrace: stack,
      tag: 'ErrorHandler',
    );
    if (!kDebugMode) {
      _withCrashlytics((crashlytics) {
        crashlytics.recordError(error, stack);
      });
    }
  }

  static void setUser(String userId) {
    _withCrashlytics((crashlytics) {
      crashlytics.setUserIdentifier(userId);
    });
  }

  static void setEnvironment(String env) {
    _withCrashlytics((crashlytics) {
      crashlytics.setCustomKey('environment', env);
    });
  }

  /// Attaches several searchable keys to every subsequent crash report.
  ///
  /// Crashlytics only accepts String/bool/num values, so anything else is
  /// stringified rather than silently dropped.
  static void setCustomKeys(Map<String, Object?> keys) {
    _withCrashlytics((crashlytics) {
      keys.forEach((key, value) {
        if (value == null) return;
        final safeValue =
            (value is String || value is bool || value is num)
                ? value
                : value.toString();
        crashlytics.setCustomKey(key, safeValue);
      });
    });
  }

  static void logBreadcrumb(String action, {Map<String, dynamic>? metadata}) {
    AppLogger.info(
      'Breadcrumb: $action',
      tag: 'ErrorHandler',
      metadata: metadata,
    );
    if (!kDebugMode) {
      _withCrashlytics((crashlytics) {
        crashlytics.log('$action ${metadata ?? ''}');
      });
    }
  }

  static void _withCrashlytics(
    void Function(FirebaseCrashlytics crashlytics) callback,
  ) {
    if (!FirebaseRuntime.crashlyticsSupported) return;
    callback(FirebaseCrashlytics.instance);
  }

  static String userMessage(AppException e) {
    switch (e.code) {
      case ErrorCode.networkUnavailable:
        return "Couldn't reach Buddy. Check your connection and try again.";
      case ErrorCode.requestTimeout:
        return "Buddy took too long to respond. Mind trying again?";
      case ErrorCode.serverError:
        return "Something went wrong on Buddy's end. Try again in a moment.";
      case ErrorCode.unauthorized:
      case ErrorCode.authFailed:
      case ErrorCode.authCancelled:
      case ErrorCode.authTokenExpired:
        // message is set at the call site with context-specific copy
        return e.message;
      case ErrorCode.firestoreReadFailed:
      case ErrorCode.firestoreWriteFailed:
      case ErrorCode.documentNotFound:
      case ErrorCode.notFound:
      case ErrorCode.forbidden:
      case ErrorCode.unexpected:
      case ErrorCode.unknown:
        return 'Something went wrong. Try again in a moment.';
    }
  }
}
