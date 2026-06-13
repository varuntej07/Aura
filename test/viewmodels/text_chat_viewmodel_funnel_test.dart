import 'package:flutter_test/flutter_test.dart';
import 'package:mockito/annotations.dart';
import 'package:mockito/mockito.dart';

import 'package:aura/core/analytics/funnel_events.dart';
import 'package:aura/core/network/api_response.dart';
import 'package:aura/core/network/connectivity_service.dart';
import 'package:aura/data/local/app_database.dart';
import 'package:aura/data/repositories/agent_suggestion_pills_repository.dart';
import 'package:aura/data/repositories/chat_repository.dart';
import 'package:aura/data/services/backend_api_service.dart';
import 'package:aura/data/services/buddy_pills_refresher.dart';
import 'package:aura/data/services/chat_backup_service.dart';
import 'package:aura/data/services/chat_service_provider.dart';
import 'package:aura/data/services/chat_session_manager.dart';
import 'package:aura/data/services/feedback_service.dart';
import 'package:aura/data/services/posthog_analytics_service.dart';
import 'package:aura/presentation/viewmodels/text_chat_viewmodel.dart';

import 'text_chat_viewmodel_funnel_test.mocks.dart';

/// Behavioral coverage for the client end of the re-engagement funnel:
/// `loadSignalNotificationContext` (session step) and the first-reply arming in
/// `sendMessage` (action step). The action event MUST fire exactly once per
/// notification-opened thread — a double-fire corrupts step-4 conversion.
@GenerateNiceMocks([
  MockSpec<ChatServiceProvider>(),
  MockSpec<ConnectivityService>(),
  MockSpec<ChatRepository>(),
  MockSpec<ChatBackupService>(),
  MockSpec<FeedbackService>(),
  MockSpec<ChatSessionManager>(),
  MockSpec<PostHogAnalyticsService>(),
  MockSpec<AgentSuggestionPillsRepository>(),
  MockSpec<BuddyPillsRefresher>(),
])
void _stubStream(MockChatServiceProvider chatService, List<ChatStreamEvent> events) {
  when(
    chatService.sendMessageStream(
      any,
      any,
      history: anyNamed('history'),
      sessionId: anyNamed('sessionId'),
      clientMessageId: anyNamed('clientMessageId'),
      agentId: anyNamed('agentId'),
      attachments: anyNamed('attachments'),
    ),
  ).thenAnswer((_) => Stream.fromIterable(events));
}

void main() {
  setUpAll(() {
    provideDummy<Result<void>>(const Result.success(null));
    provideDummy<Result<List<ChatSession>>>(const Result.success(<ChatSession>[]));
  });

  late MockChatServiceProvider chatService;
  late MockConnectivityService connectivity;
  late MockChatRepository chatRepository;
  late MockChatBackupService backupService;
  late MockFeedbackService feedbackService;
  late MockChatSessionManager sessionManager;
  late MockPostHogAnalyticsService postHog;
  late TextChatViewModel vm;

  setUp(() {
    chatService = MockChatServiceProvider();
    connectivity = MockConnectivityService();
    chatRepository = MockChatRepository();
    backupService = MockChatBackupService();
    feedbackService = MockFeedbackService();
    sessionManager = MockChatSessionManager();
    postHog = MockPostHogAnalyticsService();

    when(connectivity.statusStream).thenAnswer((_) => const Stream.empty());
    when(connectivity.isConnected).thenAnswer((_) async => true);
    when(chatRepository.saveMessage(any, userId: anyNamed('userId')))
        .thenAnswer((_) async => const Result.success(null));
    when(chatRepository.getSessionsForAgent(
      userId: anyNamed('userId'),
      agentId: anyNamed('agentId'),
    )).thenAnswer((_) async => const Result.success(<ChatSession>[]));
    when(postHog.trackEvent(any, properties: anyNamed('properties')))
        .thenAnswer((_) async {});

    vm = TextChatViewModel(
      backendService: chatService,
      connectivityService: connectivity,
      chatRepository: chatRepository,
      chatBackupService: backupService,
      feedbackService: feedbackService,
      chatSessionManager: sessionManager,
      postHogAnalyticsService: postHog,
      suggestionPillsRepository: MockAgentSuggestionPillsRepository(),
      buddyPillsRefresher: MockBuddyPillsRefresher(),
    );
  });

  tearDown(() => vm.dispose());

  group('loadSignalNotificationContext', () {
    test('fires signal_session_from_notification with join keys + arms reply',
        () async {
      await vm.loadSignalNotificationContext(
        notificationId: 'notif-1',
        contentId: 'content-1',
        category: 'tech',
        initialMessage: 'Saw this and thought of you.',
      );
      await pumpEventQueue();

      // Opener persisted as an assistant bubble.
      expect(vm.messages, hasLength(1));
      expect(vm.messages.first.isUser, isFalse);
      expect(vm.messages.first.text, 'Saw this and thought of you.');

      final captured = verify(postHog.trackEvent(
        FunnelEvents.sessionFromNotification,
        properties: captureAnyNamed('properties'),
      )).captured.single as Map<String, Object>;
      expect(captured[FunnelEvents.propNotificationId], 'notif-1');
      expect(captured[FunnelEvents.propContentId], 'content-1');
      expect(captured[FunnelEvents.propCategory], 'tech');
      expect(captured[FunnelEvents.propNotificationOrigin],
          FunnelEvents.originSignalEngine);
    });

    test('empty initialMessage persists no opener bubble', () async {
      await vm.loadSignalNotificationContext(
        notificationId: 'notif-1',
        contentId: 'content-1',
        category: 'tech',
        initialMessage: '',
      );
      await pumpEventQueue();

      expect(vm.messages, isEmpty);
      // Session event still fires — the thread opened from the notification.
      verify(postHog.trackEvent(
        FunnelEvents.sessionFromNotification,
        properties: anyNamed('properties'),
      )).called(1);
    });
  });

  group('sendMessage — action event arming', () {
    test('fires signal_action_after_notification exactly once when armed',
        () async {
      _stubStream(chatService, [DoneEvent()]);

      await vm.loadSignalNotificationContext(
        notificationId: 'notif-1',
        contentId: 'content-1',
        category: 'tech',
        initialMessage: 'opener',
      );
      await pumpEventQueue();

      await vm.sendMessage('first reply', 'uid-1');
      await pumpEventQueue();

      final captured = verify(postHog.trackEvent(
        FunnelEvents.actionAfterNotification,
        properties: captureAnyNamed('properties'),
      )).captured.single as Map<String, Object>;
      expect(captured[FunnelEvents.propNotificationId], 'notif-1');
      expect(captured[FunnelEvents.propContentId], 'content-1');
      expect(captured[FunnelEvents.propCategory], 'tech');
      expect(captured[FunnelEvents.propNotificationOrigin],
          FunnelEvents.originSignalEngine);
    });

    test('CRITICAL: second reply does not re-fire the action event (disarms)',
        () async {
      _stubStream(chatService, [DoneEvent()]);

      await vm.loadSignalNotificationContext(
        notificationId: 'notif-1',
        contentId: 'content-1',
        category: 'tech',
        initialMessage: 'opener',
      );
      await pumpEventQueue();

      await vm.sendMessage('first reply', 'uid-1');
      await pumpEventQueue();
      await vm.sendMessage('second reply', 'uid-1');
      await pumpEventQueue();

      verify(postHog.trackEvent(
        FunnelEvents.actionAfterNotification,
        properties: anyNamed('properties'),
      )).called(1);
    });

    test('no action event when the thread was not opened from a notification',
        () async {
      _stubStream(chatService, [DoneEvent()]);

      await vm.sendMessage('hi', 'uid-1');
      await pumpEventQueue();

      verifyNever(postHog.trackEvent(
        FunnelEvents.actionAfterNotification,
        properties: anyNamed('properties'),
      ));
    });
  });

  group('loadIcebreakerContext', () {
    test('renders opener bubble, fires session, arms reply once', () async {
      _stubStream(chatService, [DoneEvent()]);

      await vm.loadIcebreakerContext(
        notificationId: 'ib-1',
        openingMessage: "How's Bruno doing?",
      );
      await pumpEventQueue();

      // The opener actually renders — the exact bug that left the chat empty
      // because chat_screen had no icebreaker branch and no load method existed.
      expect(vm.messages, hasLength(1));
      expect(vm.messages.first.isUser, isFalse);
      expect(vm.messages.first.text, "How's Bruno doing?");

      final session = verify(postHog.trackEvent(
        FunnelEvents.icebreakerSessionFromNotification,
        properties: captureAnyNamed('properties'),
      )).captured.single as Map<String, Object>;
      expect(session[FunnelEvents.propNotificationId], 'ib-1');
      expect(session[FunnelEvents.propNotificationOrigin],
          FunnelEvents.originIcebreaker);

      await vm.sendMessage('he is great', 'uid-1');
      await pumpEventQueue();
      await vm.sendMessage('second reply', 'uid-1');
      await pumpEventQueue();

      // Reply step fires exactly once, then disarms (no double-count).
      final reply = verify(postHog.trackEvent(
        FunnelEvents.icebreakerReply,
        properties: captureAnyNamed('properties'),
      )).captured.single as Map<String, Object>;
      expect(reply[FunnelEvents.propNotificationId], 'ib-1');
      expect(reply[FunnelEvents.propNotificationOrigin],
          FunnelEvents.originIcebreaker);
    });
  });

  group('loadThreadFollowUpContext — unified arming parity', () {
    test('fires thread session + arms thread reply when no shade exchange',
        () async {
      _stubStream(chatService, [DoneEvent()]);

      await vm.loadThreadFollowUpContext(
        threadId: 'thr-1',
        question: "what's that about?",
        suggestedReplies: const ['tell me more'],
      );
      await pumpEventQueue();

      verify(postHog.trackEvent(
        FunnelEvents.threadSessionFromNotification,
        properties: anyNamed('properties'),
      )).called(1);

      await vm.sendMessage('it is a side project', 'uid-1');
      await pumpEventQueue();

      verify(postHog.trackEvent(
        FunnelEvents.threadReply,
        properties: anyNamed('properties'),
      )).called(1);
    });

    test('does NOT arm thread reply when already answered in the shade',
        () async {
      _stubStream(chatService, [DoneEvent()]);

      await vm.loadThreadFollowUpContext(
        threadId: 'thr-1',
        question: "what's that about?",
        suggestedReplies: const [],
        priorMessages: const [
          {'role': 'assistant', 'content': "what's that about?"},
          {'role': 'user', 'content': 'already answered in shade'},
        ],
      );
      await pumpEventQueue();

      await vm.sendMessage('another message', 'uid-1');
      await pumpEventQueue();

      // Shade replies are counted server-side; arming would double-count.
      verifyNever(postHog.trackEvent(
        FunnelEvents.threadReply,
        properties: anyNamed('properties'),
      ));
    });
  });
}
