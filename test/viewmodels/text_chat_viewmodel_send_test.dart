import 'package:flutter_test/flutter_test.dart';
import 'package:mockito/annotations.dart';
import 'package:mockito/mockito.dart';

import 'package:aura/core/errors/app_exception.dart';
import 'package:aura/core/network/api_response.dart';
import 'package:aura/core/network/connectivity_service.dart';
import 'package:aura/data/local/app_database.dart';
import 'package:aura/data/models/chat_message_model.dart';
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
import 'package:aura/presentation/viewmodels/view_state.dart';

import 'text_chat_viewmodel_send_test.mocks.dart';

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
/// Stubs `sendMessageStream` (all-matcher form) to emit [events].
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

  test('empty text with no attachments is a no-op', () async {
    await vm.sendMessage('   ', 'uid-1');
    await pumpEventQueue();

    expect(vm.messages, isEmpty);
    verifyNever(chatService.sendMessageStream(
      any,
      any,
      history: anyNamed('history'),
      sessionId: anyNamed('sessionId'),
      clientMessageId: anyNamed('clientMessageId'),
      agentId: anyNamed('agentId'),
      attachments: anyNamed('attachments'),
    ));
  });

  test('happy path: deltas accumulate, DoneEvent finalizes assistant message',
      () async {
    _stubStream(chatService, [
      TextDeltaEvent('Hel'),
      TextDeltaEvent('lo'),
      DoneEvent(),
    ]);

    await vm.sendMessage('hi', 'uid-1');
    await pumpEventQueue();

    expect(vm.messages, hasLength(2));
    expect(vm.messages.first.isUser, isTrue);
    final assistant = vm.messages.last;
    expect(assistant.isUser, isFalse);
    expect(assistant.text, 'Hello');
    expect(vm.state, ViewState.loaded);
    expect(vm.isStreaming, isFalse);
    expect(vm.streamingText, '');
    verify(postHog.trackEvent('chat_message_sent',
        properties: anyNamed('properties'))).called(1);
  });

  test('DoneEvent with reminder metadata attaches a reminder payload', () async {
    _stubStream(chatService, [
      TextDeltaEvent('Set!'),
      DoneEvent(metadata: const {
        'reminder': {
          'reminder_id': 'r1',
          'message': 'Call mom',
          'trigger_at': '2026-06-01T10:00:00Z',
          'status': 'pending',
          'priority': 'normal',
        },
      }),
    ]);

    await vm.sendMessage('remind me', 'uid-1');
    await pumpEventQueue();

    expect(vm.messages.last.reminderPayload, isNotNull);
    expect(vm.messages.last.reminderPayload!.message, 'Call mom');
  });

  test('ErrorStreamEvent surfaces ONLY the error bubble, never the banner',
      () async {
    _stubStream(chatService, [ErrorStreamEvent('overloaded')]);

    await vm.sendMessage('hi', 'uid-1');
    await pumpEventQueue();

    final last = vm.messages.last;
    expect(last.isUser, isFalse);
    expect(last.status, MessageStatus.error);
    expect(last.errorReason, isNotNull);
    // A stream failure must not also set the banner error (vm.error), or the
    // user sees two error messages at once and retry clears only the bubble.
    expect(vm.error, isNull);
    expect(vm.state, ViewState.loaded);
  });

  test('stream onError surfaces ONLY the error bubble, never the banner',
      () async {
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
    ).thenAnswer((_) => Stream.error(AppException.serverError(500, '')));

    await vm.sendMessage('hi', 'uid-1');
    await pumpEventQueue();

    final last = vm.messages.last;
    expect(last.isUser, isFalse);
    expect(last.status, MessageStatus.error);
    expect(last.errorReason, isNotNull);
    expect(vm.error, isNull);
    expect(vm.state, ViewState.loaded);
  });

  test('retryLastMessage removes the error bubble and clears any banner error',
      () async {
    _stubStream(chatService, [ErrorStreamEvent('overloaded')]);
    when(chatRepository.deleteMessage(any))
        .thenAnswer((_) async => const Result.success(null));

    await vm.sendMessage('hi', 'uid-1');
    await pumpEventQueue();
    final errorBubble = vm.messages.last;
    expect(errorBubble.status, MessageStatus.error);

    // Retry succeeds this time.
    _stubStream(chatService, [TextDeltaEvent('Hello'), DoneEvent()]);
    await vm.retryLastMessage(errorBubble.id);
    await pumpEventQueue();

    expect(vm.messages.any((m) => m.status == MessageStatus.error), isFalse);
    expect(vm.messages.last.text, 'Hello');
    expect(vm.error, isNull);
    expect(vm.state, ViewState.loaded);
  });

  test('ChatLimitReachedEvent sets the flag and can be cleared', () async {
    _stubStream(chatService, [ChatLimitReachedEvent('limit')]);

    await vm.sendMessage('hi', 'uid-1');
    await pumpEventQueue();

    expect(vm.chatLimitReached, isTrue);
    expect(vm.isStreaming, isFalse);

    vm.clearChatLimitReached();
    expect(vm.chatLimitReached, isFalse);
  });

  test('ClarificationUiEvent adds a clarification message', () async {
    _stubStream(chatService, [
      ClarificationUiEvent(
        clarificationId: 'c1',
        question: 'Which one?',
        options: const ['A', 'B'],
        multiSelect: false,
      ),
    ]);

    await vm.sendMessage('ambiguous', 'uid-1');
    await pumpEventQueue();

    expect(vm.messages.last.clarificationPayload, isNotNull);
    expect(vm.messages.last.clarificationPayload!.question, 'Which one?');
    expect(vm.state, ViewState.loaded);
    expect(vm.isStreaming, isFalse);
  });

  test('persist failure aborts send before streaming', () async {
    when(chatRepository.saveMessage(any, userId: anyNamed('userId')))
        .thenAnswer((_) async => Result.failure(
            AppException.unexpected('save failed')));

    await vm.sendMessage('hi', 'uid-1');
    await pumpEventQueue();

    expect(vm.messages, isEmpty);
    expect(vm.state, ViewState.error);
    verifyNever(chatService.sendMessageStream(
      any,
      any,
      history: anyNamed('history'),
      sessionId: anyNamed('sessionId'),
      clientMessageId: anyNamed('clientMessageId'),
      agentId: anyNamed('agentId'),
      attachments: anyNamed('attachments'),
    ));
  });
}
