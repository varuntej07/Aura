import 'dart:async';

import 'package:firebase_crashlytics/firebase_crashlytics.dart';
import 'package:firebase_messaging/firebase_messaging.dart';
import 'package:flutter/material.dart';
import 'package:provider/provider.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'app.dart';
import 'core/config/environment.dart';
import 'core/config/firebase_config.dart';
import 'core/errors/error_handler.dart';
import 'core/logging/app_logger.dart';
import 'data/services/analytics_service.dart';
import 'data/services/keyboard_credential_bridge.dart';
import 'data/services/posthog_analytics_service.dart';
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

      // Initialize PostHog explicitly before runApp so the earliest events are captured. 
      await PostHogAnalyticsService.initialize();

      firebaseReady = await FirebaseConfig.initialize();

      // Register the background handler before runApp so FCM can wire it up during app startup
      FirebaseMessaging.onBackgroundMessage(_firebaseMessagingBackgroundHandler);

      ErrorHandler.init();
      ErrorHandler.setEnvironment(Environment.current.env.name);

      if (firebaseReady) {
        await FirebaseCrashlytics.instance.setCrashlyticsCollectionEnabled(true);
        unawaited(AnalyticsService.logAppOpen());
        // Keep the Buddy Keyboard's shared credential in sync with the auth session
        // (sign-in / token refresh / sign-out)
        KeyboardCredentialBridge.instance.start();
      }

      // Start relaying home-screen voice-widget taps (warm launches) before the
      // first frame so a tap while the app is running is never missed. Android-only;
      // a no-op elsewhere. Cold-launch taps are read by HomeScreen on mount.
      VoiceLauncherBridge.instance.start();

      // Start listening for aura://voice deep links before the first frame; 
      // cold-launch links are read by HomeScreen on mount.
      DeepLinkService.instance.start();

      AppLogger.info(
        'Aura starting',
        tag: 'main',
        metadata: {
          'env': Environment.current.env.name,
          'firebase_ready': firebaseReady,
        },
      );

      final prefs = await SharedPreferences.getInstance();
      runApp(MultiProvider(providers: buildProviders(prefs), child: const AuraApp()));
    },
    (error, stack) {
      AppLogger.error(
        'Uncaught async error',
        error: error,
        stackTrace: stack,
        tag: 'main',
      );
      if (firebaseReady && !Environment.isDev) {
        FirebaseCrashlytics.instance.recordError(error, stack, fatal: true);
      }
    },
  );
}
