import 'package:firebase_core/firebase_core.dart';
import 'package:flutter/foundation.dart';

class FirebaseRuntime {
  FirebaseRuntime._();

  static bool get hasApp {
    try {
      return Firebase.apps.isNotEmpty;
    } catch (_) {
      return false;
    }
  }

  /// Crashlytics has no Windows/Linux plugin. [hasApp] is NOT enough to call
  /// it: Firebase core initializes fine on Windows, and then
  /// FirebaseCrashlytics.instance throws a SYNCHRONOUS assertion, which can
  /// abort whatever listener it was called from (this froze the desktop
  /// sign-in flow mid-stream during M3 testing). Every Crashlytics call site
  /// must gate on this, not on [hasApp] alone.
  static bool get crashlyticsSupported =>
      hasApp &&
      !kIsWeb &&
      (defaultTargetPlatform == TargetPlatform.android ||
          defaultTargetPlatform == TargetPlatform.iOS ||
          defaultTargetPlatform == TargetPlatform.macOS);
}
