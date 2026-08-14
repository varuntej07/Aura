import 'dart:async';

import 'package:firebase_crashlytics/firebase_crashlytics.dart';
import 'package:firebase_messaging/firebase_messaging.dart';
import 'package:flutter/material.dart';
import 'package:provider/provider.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'app.dart';
import 'core/config/environment.dart';
import 'core/config/firebase_config.dart';
import 'core/config/firebase_runtime.dart';
import 'core/errors/error_handler.dart';
import 'core/logging/app_logger.dart';
import 'data/services/analytics_service.dart';
import 'data/services/keyboard_credential_bridge.dart';
import 'data/services/notification_service.dart' show kEntitlementUpdatedType;
import 'data/services/posthog_analytics_service.dart';
import 'data/services/startup_diagnostics_service.dart';
import 'data/services/subscription_service.dart' show kEntitlementRefreshPendingKey;
import 'presentation/screens/startup_failure_app.dart';
import 'data/services/thread_notification_handler.dart';
import 'data/services/voice_launcher_bridge.dart';
import 'data/services/deep_link_service.dart';
import 'di/providers.dart';

/// FCM background message handler.
/// Must be a top-level function (Flutter / isolate constraint)
@pragma('vm:entry-point')
Future<void> _firebaseMessagingBackgroundHandler(RemoteMessage message) async {
  // Firebase must be re-initialized in background isolates
  await FirebaseConfig.initialize();
  AppLogger.info(
    'FCM background message received',
    tag: 'FCM',
    metadata: {
      'messageId': message.messageId,
      'notificationType': message.data['notification_type'],
    },
  );

  // Curiosity follow-ups arrive data-only so we can render interactive
  // suggestion chips ourselves (FCM cannot draw action buttons).
  // Build the rich notification here in the background isolate.
  if (isThreadFollowUp(message)) {
    await showThreadFollowUpNotification(message);
  }

  // Billing sync push while backgrounded: this isolate cannot reach the UI
  // isolate's streams, so persist a marker that SubscriptionService consumes
  // on the next app resume. Without it a refund/renewal landing while the app
  // is backgrounded stays invisible until a restart or paywall visit.
  if (message.data['notification_type'] == kEntitlementUpdatedType) {
    final prefs = await SharedPreferences.getInstance();
    await prefs.setBool(kEntitlementRefreshPendingKey, true);
  }
}

/// Ceiling on any single startup step.
///
/// A step that hangs is indistinguishable to the user from a step that crashed:
/// both leave a dead window. Every await before `runApp` is bounded so a wedged
/// plugin degrades to a missing feature instead of an app that never opens.
const Duration _startupStepTimeout = Duration(seconds: 10);

/// Runs one startup step so that it can never prevent the app from opening.
///
/// This app previously awaited PostHog setup, Firebase init and SharedPreferences
/// unguarded, in that order, before calling `runApp`. Any throw or hang in any of
/// them meant `runApp` was never reached and the process sat as a dead window —
/// and because Crashlytics was armed midway through that same sequence, the
/// earliest failures were structurally unreportable. Each step is now isolated:
/// it is bounded in time, its failure is recorded, and startup continues.
Future<T?> _startupStep<T>(
  String name,
  Future<T> Function() step, {
  Duration timeout = _startupStepTimeout,
}) async {
  try {
    return await step().timeout(timeout);
  } catch (error, stack) {
    AppLogger.error(
      'Startup step failed: $name',
      error: error,
      stackTrace: stack,
      tag: 'main',
    );
    // Non-fatal: the app is still opening. Crashlytics is armed before the first
    // call site below, so this always has somewhere to go.
    ErrorHandler.handle(error, stack);
    return null;
  }
}

void main() {
  // firebaseReady is declared outside so the error handler closure can read it
  // even if an error fires before or after Firebase initializes.
  bool firebaseReady = false;

  runZonedGuarded(
    () async {
      // ensureInitialized and runApp must be in the same zone to avoid the
      // "Zone mismatch" binding assertion introduced in Flutter 3.x.
      WidgetsFlutterBinding.ensureInitialized();

      unawaited(
        StartupDiagnosticsService.stamp(StartupDiagnosticsService.stageDartMain),
      );

      // ── Crash reporting comes FIRST ────────────────────────────────────────
      // Everything below this block is a potential cause of a startup death, so
      // the reporter has to exist before any of it runs. Previously Crashlytics
      // was enabled after PostHog setup and Firebase init, which made a failure
      // in either of those impossible to see.
      firebaseReady = await _startupStep('firebase', FirebaseConfig.initialize) ?? false;

      ErrorHandler.init();
      // Carries the build mode alongside the env, so a `dev` tag on a release
      // binary is visible in the console instead of quietly mislabelling reports.
      ErrorHandler.setEnvironment(Environment.crashReportingTag);

      // crashlyticsSupported, not firebaseReady: Firebase core initializes fine
      // on Windows/Linux and then FirebaseCrashlytics.instance throws a
      // synchronous assertion. See FirebaseRuntime.
      if (FirebaseRuntime.crashlyticsSupported) {
        await _startupStep(
          'crashlytics',
          () => FirebaseCrashlytics.instance.setCrashlyticsCollectionEnabled(true),
        );
      }
      unawaited(
        StartupDiagnosticsService.stamp(
          StartupDiagnosticsService.stageFirebaseReady,
        ),
      );

      // If the previous launch never rendered a frame, file what the OS recorded
      // about its death. Runs immediately after Crashlytics is armed and before
      // any other risky step, so the report survives even if THIS launch also
      // dies. The native beacon has already sent the same record over raw HTTP.
      unawaited(StartupDiagnosticsService.reportPreviousLaunchFailure());

      // ── Everything else is best-effort ────────────────────────────────────
      await _startupStep('posthog', PostHogAnalyticsService.initialize);
      unawaited(
        StartupDiagnosticsService.stamp(
          StartupDiagnosticsService.stagePostHogReady,
        ),
      );

      await _startupStep('bridges', () async {
        // Register the background handler before runApp so FCM can wire it up
        // during app startup
        if (firebaseReady) {
          FirebaseMessaging.onBackgroundMessage(_firebaseMessagingBackgroundHandler);
          unawaited(AnalyticsService.logAppOpen());
          // Keep the Buddy Keyboard's shared credential in sync with the auth
          // session (sign-in / token refresh / sign-out)
          KeyboardCredentialBridge.instance.start();
        }

        // Start relaying home-screen voice-widget taps (warm launches) before the
        // first frame so a tap while the app is running is never missed. Android-only;
        // a no-op elsewhere. Cold-launch taps are read by HomeScreen on mount.
        VoiceLauncherBridge.instance.start();

        // Start listening for aura://voice deep links before the first frame;
        // cold-launch links are read by HomeScreen on mount.
        DeepLinkService.instance.start();
      });
      unawaited(
        StartupDiagnosticsService.stamp(
          StartupDiagnosticsService.stageBridgesReady,
        ),
      );

      AppLogger.info(
        'Aura starting',
        tag: 'main',
        metadata: {
          'env': Environment.current.env.name,
          'build_mode': Environment.buildMode,
          'firebase_ready': firebaseReady,
        },
      );

      final prefs = await _startupStep('prefs', SharedPreferences.getInstance);
      unawaited(
        StartupDiagnosticsService.stamp(
          StartupDiagnosticsService.stagePrefsReady,
        ),
      );

      if (prefs == null) {
        // SharedPreferences is the one startup dependency with no safe stand-in:
        // buildProviders needs a real instance. Rather than silently booting an
        // app whose persisted state is a lie, surface it. This is a visible,
        // reportable failure instead of a window that closes itself.
        AppLogger.error(
          'SharedPreferences unavailable; starting in degraded mode',
          tag: 'main',
        );
        unawaited(
          StartupDiagnosticsService.stamp(StartupDiagnosticsService.stageRunApp),
        );
        runApp(const StartupFailureApp(failedStep: 'preferences'));
        return;
      }

      unawaited(
        StartupDiagnosticsService.stamp(StartupDiagnosticsService.stageRunApp),
      );
      runApp(MultiProvider(providers: buildProviders(prefs), child: const AuraApp()));
    },
    (error, stack) {
      AppLogger.error(
        'Uncaught async error',
        error: error,
        stackTrace: stack,
        tag: 'main',
      );
      // Gated on the BUILD MODE, not on Environment.isDev. `ENV` is a
      // --dart-define that no build command in this repo passes, so isDev was
      // true in every shipped Play build and this reporter — the only one
      // covering uncaught errors during startup — never fired in production.
      //
      // recordFatal (not FirebaseCrashlytics.instance directly) because the
      // instance getter throws a synchronous assertion on Windows/Linux, where
      // Crashlytics has no plugin. The old isDev gate hid that by never firing
      // at all; this one has to guard it properly.
      if (firebaseReady) {
        ErrorHandler.recordFatal(error, stack);
      }
    },
  );
}
