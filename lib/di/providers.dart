import 'package:provider/provider.dart';
import 'package:provider/single_child_widget.dart';
import 'package:shared_preferences/shared_preferences.dart';

import '../core/config/environment.dart';
import '../core/network/api_client.dart';
import '../core/network/connectivity_service.dart';
import '../data/local/app_database.dart';
import '../data/repositories/agent_suggestion_pills_repository.dart';
import '../data/repositories/auth_repository.dart';
import '../data/repositories/chat_repository.dart';
import '../data/repositories/memory_repository.dart';
import '../data/repositories/onboarding_repository.dart';
import '../data/repositories/reminder_repository.dart';
import '../data/services/app_feedback_service.dart';
import '../data/services/chat_backup_service.dart';
import '../data/services/chat_session_manager.dart';
import '../data/services/feedback_service.dart';
import '../data/services/firebase_auth_service.dart';
import '../data/services/firestore_service.dart';
import '../data/services/connectors_service.dart';
import '../data/services/backend_api_service.dart';
import '../data/services/buddy_pills_refresher.dart';
import '../data/services/session_consolidator.dart';
import '../data/services/chat_service_provider.dart';
import '../data/services/stub_chat_service_provider.dart';
import '../data/services/notification_service.dart';
import '../data/services/posthog_analytics_service.dart';
import '../data/services/voice_session_service.dart';
import '../data/services/wake_word_service.dart';
import '../data/services/subscription_service.dart';
import '../presentation/viewmodels/auth_viewmodel.dart';
import '../presentation/viewmodels/connectors_viewmodel.dart';
import '../presentation/viewmodels/home_viewmodel.dart';
import '../presentation/viewmodels/reminders_viewmodel.dart';
import '../presentation/viewmodels/settings_viewmodel.dart';
import '../presentation/viewmodels/subscription_viewmodel.dart';

List<SingleChildWidget> buildProviders(SharedPreferences prefs) {
  // Infrastructure
  final firebaseAuthService = FirebaseAuthService();
  final firestoreService = FirestoreService();
  final connectivityService = ConnectivityService();
  final apiClient = ApiClient(
    connectivity: connectivityService,
    tokenProvider: firebaseAuthService.getIdToken,
  );

  // Local database (singleton: lives for the lifetime of the app)
  final appDatabase = AppDatabase();
  final chatBackupService = ChatBackupService(db: appDatabase);
  final feedbackService = FeedbackService();
  final chatRepository = ChatRepository(
    db: appDatabase,
    chatBackupService: chatBackupService,
  );
  final chatSessionManager = ChatSessionManager(repository: chatRepository);

  // Analytics
  final postHogAnalyticsService = PostHogAnalyticsService();

  // Feedback — one write path shared by Settings and the voice orb.
  final appFeedbackService = AppFeedbackService(
    firestoreService: firestoreService,
    postHogAnalyticsService: postHogAnalyticsService,
  );

  // Remote services
  final backendApiService = BackendApiService(apiClient: apiClient);
  final buddyPillsRefresher = BuddyPillsRefresher(backendApiService: backendApiService);
  final sessionConsolidator = SessionConsolidator(backendApiService: backendApiService);
  final ChatServiceProvider chatServiceProvider = Environment.hasConfiguredApi
      ? backendApiService
      : StubChatServiceProvider();
  final connectorsService = ConnectorsService(
    apiClient: apiClient,
    authService: firebaseAuthService,
  );
  final notificationService = NotificationService(
    apiClient: apiClient,
    signalEventSink: backendApiService,
    postHogAnalyticsService: postHogAnalyticsService,
  );
  final voiceSessionService = VoiceSessionService(
    tokenProvider: firebaseAuthService.getIdToken,
    postHogAnalyticsService: postHogAnalyticsService,
  );

  final wakeWordService = WakeWordService();
  final subscriptionService = SubscriptionService(
    firestoreService: firestoreService,
    authService: firebaseAuthService,
    postHogAnalyticsService: postHogAnalyticsService,
  );

  // Domain repositories
  final authRepository = AuthRepository(
    authService: firebaseAuthService,
    firestoreService: firestoreService,
  );
  final memoryRepository = MemoryRepository(firestoreService: firestoreService);
  final reminderRepository = ReminderRepository(
    firestoreService: firestoreService,
  );
  final agentSuggestionPillsRepository = AgentSuggestionPillsRepository(
    firestoreService: firestoreService,
  );
  final onboardingRepository = OnboardingRepository(
    firestoreService: firestoreService,
    postHogAnalyticsService: postHogAnalyticsService,
    backendApiService: backendApiService,
  );

  return [
    // Analytics
    Provider<PostHogAnalyticsService>.value(value: postHogAnalyticsService),

    // Infrastructure
    Provider<FirebaseAuthService>.value(value: firebaseAuthService),
    Provider<FirestoreService>.value(value: firestoreService),
    Provider<ConnectivityService>.value(value: connectivityService),
    Provider<ApiClient>.value(value: apiClient),

    // Local database
    Provider<AppDatabase>.value(value: appDatabase),
    Provider<ChatBackupService>.value(value: chatBackupService),
    Provider<FeedbackService>.value(value: feedbackService),
    Provider<AppFeedbackService>.value(value: appFeedbackService),
    Provider<ChatRepository>.value(value: chatRepository),
    Provider<ChatSessionManager>.value(value: chatSessionManager),

    // Remote services
    Provider<NotificationService>.value(value: notificationService),
    Provider<BackendApiService>.value(value: backendApiService),
    Provider<BuddyPillsRefresher>.value(value: buddyPillsRefresher),
    Provider<SessionConsolidator>.value(value: sessionConsolidator),
    Provider<ChatServiceProvider>.value(value: chatServiceProvider),
    Provider<ConnectorsService>.value(
      value: connectorsService,
    ),
    Provider<VoiceSessionService>.value(value: voiceSessionService),
    Provider<WakeWordService>.value(value: wakeWordService),
    ChangeNotifierProvider<SubscriptionService>.value(value: subscriptionService),

    // Domain repositories
    Provider<AuthRepository>.value(value: authRepository),
    Provider<MemoryRepository>.value(value: memoryRepository),
    Provider<ReminderRepository>.value(value: reminderRepository),
    Provider<AgentSuggestionPillsRepository>.value(value: agentSuggestionPillsRepository),
    Provider<OnboardingRepository>.value(value: onboardingRepository),

    // ViewModels
    ChangeNotifierProvider<AuthViewModel>(
      create: (_) => AuthViewModel(
        authRepository: authRepository,
        notificationService: notificationService,
        backendApiService: backendApiService,
        postHogAnalyticsService: postHogAnalyticsService,
      ),
    ),
    ChangeNotifierProvider<HomeViewModel>(
      create: (_) => HomeViewModel(
        voiceSessionService: voiceSessionService,
        wakeWordService: wakeWordService,
        chatRepository: chatRepository,
        notificationService: notificationService,
        appFeedbackService: appFeedbackService,
        buddyPillsRefresher: buddyPillsRefresher,
      ),
    ),
    ChangeNotifierProvider<SettingsViewModel>(
      create: (_) => SettingsViewModel(
        firestoreService: firestoreService,
        appFeedbackService: appFeedbackService,
      ),
    ),
    ChangeNotifierProvider<ConnectorsViewModel>(
      create: (_) => ConnectorsViewModel(connectorService: connectorsService),
      ),
    ChangeNotifierProvider<SubscriptionViewModel>(
      create: (_) => SubscriptionViewModel(subscriptionService: subscriptionService),
    ),
    ChangeNotifierProvider<RemindersViewModel>(
      create: (_) => RemindersViewModel(repository: reminderRepository),
    ),
  ];
}
