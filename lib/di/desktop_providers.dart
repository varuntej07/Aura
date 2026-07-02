import 'package:provider/provider.dart';
import 'package:provider/single_child_widget.dart';
import 'package:shared_preferences/shared_preferences.dart';

import '../core/analytics/analytics_client.dart';
import '../core/analytics/posthog_http_analytics.dart';
import '../core/config/environment.dart';
import '../core/network/api_client.dart';
import '../core/network/connectivity_service.dart';
import '../data/local/app_database.dart';
import '../data/repositories/auth_repository.dart';
import '../data/repositories/chat_repository.dart';
import '../data/services/backend_api_service.dart';
import '../data/services/chat_backup_service.dart';
import '../data/services/chat_service_provider.dart';
import '../data/services/desktop/desktop_notification_service_stub.dart';
import '../data/services/desktop/desktop_screen_capture_service.dart';
import '../data/services/desktop/overlay_controller.dart';
import '../data/services/desktop/pairing_service.dart';
import '../data/services/desktop/pointing_overlay_service.dart';
import '../data/services/desktop/screen_demo_service.dart';
import '../data/services/desktop/screen_sight_service.dart';
import '../data/services/desktop/window_effects_service.dart';
import '../data/services/firebase_auth_service.dart';
import '../data/services/firestore_service.dart';
import '../data/services/notification_service.dart';
import '../data/services/stub_chat_service_provider.dart';
import '../data/services/voice_session_service.dart';
import '../presentation/viewmodels/auth_viewmodel.dart';
import '../presentation/viewmodels/desktop_voice_viewmodel.dart';

/// Desktop DI graph (plan: distributed-humming-perlis). The mirror of
/// buildProviders in providers.dart with every Windows-absent plugin surface
/// left out: no FCM NotificationService (stubbed), no posthog_flutter (HTTP
/// capture instead), no in_app_purchase, no crashlytics/analytics, no
/// WakeWordService, no Android bridges.
List<SingleChildWidget> buildDesktopProviders(
  SharedPreferences prefs, {
  required OverlayController overlayController,
  required ScreenSightService screenSightService,
  required WindowEffectsService windowEffectsService,
}) {
  // Analytics: same PostHog project as mobile, HTTP transport.
  final analyticsClient = PostHogHttpAnalytics(prefs: prefs);

  // Infrastructure
  final firebaseAuthService = FirebaseAuthService();
  final firestoreService = FirestoreService();
  final connectivityService = ConnectivityService();
  final apiClient = ApiClient(
    connectivity: connectivityService,
    tokenProvider: firebaseAuthService.getIdToken,
  );

  // Local database (voice transcripts persist here from M2)
  final appDatabase = AppDatabase();
  final chatBackupService = ChatBackupService(db: appDatabase);
  final chatRepository = ChatRepository(
    db: appDatabase,
    chatBackupService: chatBackupService,
  );

  // Remote services
  final backendApiService = BackendApiService(apiClient: apiClient);
  final ChatServiceProvider chatServiceProvider = Environment.hasConfiguredApi
      ? backendApiService
      : StubChatServiceProvider();
  final notificationServiceStub = DesktopNotificationServiceStub(
    apiClient: apiClient,
    postHogAnalyticsService: analyticsClient,
  );
  final voiceSessionService = VoiceSessionService(
    tokenProvider: firebaseAuthService.getIdToken,
    postHogAnalyticsService: analyticsClient,
  );

  // Screen sight: the service was created at boot (its hotkey wires before
  // DI); here it gains its capture + transport dependencies. The pointing
  // service registers itself as the controller's cancel-pointing handler.
  final screenCaptureService = DesktopScreenCaptureService();
  final pointingOverlayService =
      PointingOverlayService(overlayController: overlayController);
  screenSightService.attach(
    voiceService: voiceSessionService,
    captureService: screenCaptureService,
    pointingService: pointingOverlayService,
  );
  final screenDemoService = ScreenDemoService(
    apiClient: apiClient,
    captureService: screenCaptureService,
    pointingService: pointingOverlayService,
  );

  // Domain repositories
  final authRepository = AuthRepository(
    authService: firebaseAuthService,
    firestoreService: firestoreService,
  );

  return [
    Provider<AnalyticsClient>.value(value: analyticsClient),
    Provider<FirebaseAuthService>.value(value: firebaseAuthService),
    Provider<FirestoreService>.value(value: firestoreService),
    Provider<ConnectivityService>.value(value: connectivityService),
    Provider<ApiClient>.value(value: apiClient),
    Provider<AppDatabase>.value(value: appDatabase),
    Provider<ChatBackupService>.value(value: chatBackupService),
    Provider<ChatRepository>.value(value: chatRepository),
    Provider<BackendApiService>.value(value: backendApiService),
    Provider<ChatServiceProvider>.value(value: chatServiceProvider),
    Provider<NotificationService>.value(value: notificationServiceStub),
    Provider<VoiceSessionService>.value(value: voiceSessionService),
    Provider<AuthRepository>.value(value: authRepository),
    Provider<PairingService>(create: (_) => PairingService()),
    ChangeNotifierProvider<OverlayController>.value(value: overlayController),
    ChangeNotifierProvider<WindowEffectsService>.value(
        value: windowEffectsService),
    ChangeNotifierProvider<ScreenSightService>.value(value: screenSightService),
    ChangeNotifierProvider<PointingOverlayService>.value(
        value: pointingOverlayService),
    ChangeNotifierProvider<ScreenDemoService>.value(value: screenDemoService),
    ChangeNotifierProvider<AuthViewModel>(
      create: (_) => AuthViewModel(
        authRepository: authRepository,
        notificationService: notificationServiceStub,
        backendApiService: backendApiService,
        postHogAnalyticsService: analyticsClient,
      ),
    ),
    // lazy: false — its summon listener must exist before the first
    // hidden -> panel transition, or mic-live-on-summon misses the boot summon.
    ChangeNotifierProvider<DesktopVoiceViewModel>(
      lazy: false,
      create: (_) => DesktopVoiceViewModel(
        voiceSessionService: voiceSessionService,
        chatRepository: chatRepository,
        overlayController: overlayController,
        currentUserIdProvider: () => firebaseAuthService.currentUser?.uid,
        // Sign-out (any path: the panel button, token revocation) must end a
        // live voice session; the viewmodel watches the uid going null.
        authUserIdStream:
            firebaseAuthService.authStateStream.map((user) => user?.uid),
      ),
    ),
  ];
}
