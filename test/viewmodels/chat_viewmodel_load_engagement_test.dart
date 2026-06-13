import 'package:flutter_test/flutter_test.dart';
import 'package:mocktail/mocktail.dart';

import 'package:aura/core/network/api_response.dart';
import 'package:aura/core/network/connectivity_service.dart';
import 'package:aura/data/local/app_database.dart';
import 'package:aura/data/models/chat_message_model.dart';
import 'package:aura/data/repositories/agent_suggestion_pills_repository.dart';
import 'package:aura/data/repositories/chat_repository.dart';
import 'package:aura/data/services/buddy_pills_refresher.dart';
import 'package:aura/data/services/chat_backup_service.dart';
import 'package:aura/data/services/chat_service_provider.dart';
import 'package:aura/data/services/chat_session_manager.dart';
import 'package:aura/data/services/feedback_service.dart';
import 'package:aura/data/services/posthog_analytics_service.dart';
import 'package:aura/presentation/viewmodels/text_chat_viewmodel.dart';

// Mocks

class MockChatServiceProvider extends Mock implements ChatServiceProvider {}

class MockConnectivityService extends Mock implements ConnectivityService {}

class MockChatRepository extends Mock implements ChatRepository {}

class MockChatBackupService extends Mock implements ChatBackupService {}

class MockFeedbackService extends Mock implements FeedbackService {}

class MockChatSessionManager extends Mock implements ChatSessionManager {}

class MockPostHogAnalyticsService extends Mock implements PostHogAnalyticsService {}

class MockAgentSuggestionPillsRepository extends Mock
    implements AgentSuggestionPillsRepository {}

class MockBuddyPillsRefresher extends Mock implements BuddyPillsRefresher {}

class _FakeChatMessageModel extends Fake implements ChatMessageModel {}

// Helpers
void _registerFallbacks() {
  registerFallbackValue(_FakeChatMessageModel());
}

/// Constructs a [TextChatViewModel] with all deps mocked and the minimum stubs
/// needed to let [loadEngagementContext] complete without throwing.
TextChatViewModel _buildVm({
  required MockChatServiceProvider chatService,
  required MockConnectivityService connectivity,
  required MockChatRepository chatRepository,
  required MockChatBackupService backupService,
  required MockFeedbackService feedbackService,
  required MockChatSessionManager sessionManager,
  required MockPostHogAnalyticsService postHogAnalyticsService,
}) {
  when(() => connectivity.statusStream).thenAnswer((_) => const Stream.empty());
  when(() => connectivity.isConnected).thenAnswer((_) async => true);

  when(() => chatRepository.createSession(
        userId: any(named: 'userId'),
        agentId: any(named: 'agentId'),
      )).thenAnswer((_) async => 'test-session-id');

  when(() => chatRepository.saveMessage(
        any(),
        userId: any(named: 'userId'),
      )).thenAnswer((_) async => Result<void>.success(null));

  when(() => chatRepository.getSessionsForAgent(
        userId: any(named: 'userId'),
        agentId: any(named: 'agentId'),
      )).thenAnswer((_) async => const Result.success(<ChatSession>[]));

  return TextChatViewModel(
    backendService: chatService,
    connectivityService: connectivity,
    chatRepository: chatRepository,
    chatBackupService: backupService,
    feedbackService: feedbackService,
    chatSessionManager: sessionManager,
    postHogAnalyticsService: postHogAnalyticsService,
    suggestionPillsRepository: MockAgentSuggestionPillsRepository(),
    buddyPillsRefresher: MockBuddyPillsRefresher(),
  );
}

// Tests
void main() {
  setUpAll(_registerFallbacks);

  late MockChatServiceProvider mockChatService;
  late MockConnectivityService mockConnectivity;
  late MockChatRepository mockChatRepository;
  late MockChatBackupService mockBackupService;
  late MockFeedbackService mockFeedbackService;
  late MockChatSessionManager mockSessionManager;
  late MockPostHogAnalyticsService mockPostHog;
  late TextChatViewModel vm;

  setUp(() {
    mockChatService = MockChatServiceProvider();
    mockConnectivity = MockConnectivityService();
    mockChatRepository = MockChatRepository();
    mockBackupService = MockChatBackupService();
    mockFeedbackService = MockFeedbackService();
    mockSessionManager = MockChatSessionManager();
    mockPostHog = MockPostHogAnalyticsService();

    vm = _buildVm(
      chatService: mockChatService,
      connectivity: mockConnectivity,
      chatRepository: mockChatRepository,
      backupService: mockBackupService,
      feedbackService: mockFeedbackService,
      sessionManager: mockSessionManager,
      postHogAnalyticsService: mockPostHog,
    );
  });

  tearDown(() {
    vm.dispose();
  });

  group('loadEngagementContext — markEngagementResponded guard', () {
    test('calls markEngagementResponded when engagementId is non-empty', () async {
      when(() => mockChatService.markEngagementResponded(any()))
          .thenAnswer((_) async {});

      await vm.loadEngagementContext(
        engagementId: 'eng-abc-123',
        agentContext: 'wellness',
        initialMessage: 'How are you feeling today?',
      );

      // Allow the unawaited call to execute
      await Future<void>.delayed(Duration.zero);

      verify(() => mockChatService.markEngagementResponded('eng-abc-123')).called(1);
    });

    test('does not call markEngagementResponded when engagementId is empty', () async {
      // daily_nudge taps produce an empty engagementId — must not fire the API call
      await vm.loadEngagementContext(
        engagementId: '',
        agentContext: '',
        initialMessage: 'Good morning! Your daily briefing is ready.',
      );

      await Future<void>.delayed(Duration.zero);

      verifyNever(() => mockChatService.markEngagementResponded(any()));
    });
  });

  group('loadEngagementContext — message pre-load', () {
    test('pre-loads the initialMessage as an assistant bubble', () async {
      when(() => mockChatService.markEngagementResponded(any()))
          .thenAnswer((_) async {});

      await vm.loadEngagementContext(
        engagementId: 'eng-xyz',
        agentContext: 'sports',
        initialMessage: 'Big match tonight — want a preview?',
      );

      expect(vm.messages, hasLength(1));
      final msg = vm.messages.first;
      expect(msg.isUser, isFalse);
      expect(msg.text, 'Big match tonight — want a preview?');
    });

    test('pre-load works for daily_nudge taps (empty engagementId)', () async {
      await vm.loadEngagementContext(
        engagementId: '',
        agentContext: '',
        initialMessage: 'Good morning! Here is what is on your plate today.',
      );

      expect(vm.messages, hasLength(1));
      expect(vm.messages.first.isUser, isFalse);
      expect(vm.messages.first.text,
          'Good morning! Here is what is on your plate today.');
    });
  });
}
