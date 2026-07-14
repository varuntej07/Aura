import 'dart:async';

import 'package:flutter/foundation.dart';
import 'package:uuid/uuid.dart';

import '../../core/analytics/funnel_events.dart';
import '../../core/base/safe_change_notifier.dart';
import '../../core/constants/app_constants.dart';
import '../../core/errors/app_exception.dart';
import '../../core/errors/error_handler.dart';
import '../../core/logging/app_logger.dart';
import '../../core/network/connectivity_service.dart';
import '../../data/local/app_database.dart';
import '../../data/models/chat_attachment.dart';
import '../../data/models/chat_message_model.dart';
import '../../data/models/clarification_payload.dart';
import '../../data/models/streaming_snapshot.dart';
import '../../data/repositories/chat_repository.dart';
import '../../data/services/analytics_service.dart';
import '../../data/services/backend_api_service.dart';
import '../../data/services/chat_backup_service.dart';
import '../../data/services/chat_service_provider.dart';
import '../../data/services/chat_session_manager.dart';
import '../../data/services/feedback_service.dart';
import '../../data/services/posthog_analytics_service.dart';
import 'view_state.dart';

export 'view_state.dart';

/// A funnel "first reply" event armed by a notification-seeded chat open, fired
/// exactly once by [ChatViewModel.sendMessage] on the user's next send. One type
/// for every proactive origin so the reply step is wired in a single place.
class _PendingNotificationReply {
  final String event;
  final Map<String, Object> properties;
  const _PendingNotificationReply(this.event, this.properties);
}

/// Shared chat logic used by all chat screens (main Buddy chat and per-agent threads).
/// Subclasses provide [agentId] and implement [initializeSession] for their
/// specific session-loading strategy.
abstract class ChatViewModel extends SafeChangeNotifier {
  final ChatServiceProvider _backendService;
  final ConnectivityService _connectivityService;
  final ChatRepository _chatRepository;
  final ChatBackupService _chatBackupService;
  final FeedbackService _feedbackService;
  final ChatSessionManager _sessionManager;
  final PostHogAnalyticsService postHogAnalytics;
  final _uuid = const Uuid();

  StreamSubscription<ConnectivityStatus>? _connectivitySub;
  StreamSubscription<ChatStreamEvent>? _streamSub;

  ViewState _state = ViewState.idle;
  AppException? _error;
  final List<ChatMessageModel> _messages = [];
  List<ChatSession> _sessions = const [];
  bool _isOffline = false;
  bool _isStreaming = false;
  // Live streaming output, published per token WITHOUT notifyListeners so only
  // the streaming bubble repaints (the finalized message list and the rest of
  // the screen stay put). See [StreamingSnapshot].
  final ValueNotifier<StreamingSnapshot> _streamingOutput =
      ValueNotifier(StreamingSnapshot.empty);
  String? _currentSessionId;
  String? _currentUserId;
  bool _sessionTitleSet = false;
  bool _chatLimitReached = false;
  // The uid restore/backfill has already been attempted for, so ensureRestoredForUser
  // runs the work at most once per user even when called from several places.
  String? _restoreAttemptedForUid;

  // Set when a chat thread was opened from a proactive notification that has a
  // "first reply" funnel step (signal action / thread reply / icebreaker reply).
  // The first user reply fires it once, then clears. One field for every origin,
  // so a new decider cannot forget to wire its reply step. Engagement has no
  // reply step (it marks responded on open), so it never arms this. Thread arms
  // it only for in-chat replies — shade replies are counted server-side.
  _PendingNotificationReply? _pendingReply;

  /// Buddy-facing "why I reached out" note from a proactive-notification tap.
  String? _pendingNotificationReason;

  ChatViewModel({
    required ChatServiceProvider backendService,
    required ConnectivityService connectivityService,
    required ChatRepository chatRepository,
    required ChatBackupService chatBackupService,
    required FeedbackService feedbackService,
    required ChatSessionManager chatSessionManager,
    required PostHogAnalyticsService postHogAnalyticsService,
  })  : _backendService = backendService,
        _connectivityService = connectivityService,
        _chatRepository = chatRepository,
        _chatBackupService = chatBackupService,
        _feedbackService = feedbackService,
        _sessionManager = chatSessionManager,
        postHogAnalytics = postHogAnalyticsService {
    _connectivitySub = _connectivityService.statusStream.listen((status) {
      _isOffline = status == ConnectivityStatus.disconnected;
      safeNotifyListeners();
    });
    _primeConnectivityState();
  }

  // Getters

  ViewState get state => _state;
  AppException? get error => _error;
  List<ChatMessageModel> get messages => List.unmodifiable(_messages);
  List<ChatSession> get sessions => List.unmodifiable(_sessions);
  bool get isOffline => _isOffline;
  bool get isStreaming => _isStreaming;

  /// Listenable stream of the in-flight assistant turn. The chat list binds the
  /// streaming bubble to this so a token repaints only that bubble.
  ValueListenable<StreamingSnapshot> get streamingOutput => _streamingOutput;
  String get streamingText => _streamingOutput.value.text;
  String? get thinkingMessage => _streamingOutput.value.thinkingMessage;
  String? get currentSessionId => _currentSessionId;

  /// True when the backend reports the free-tier daily chat limit has been hit.
  /// The UI should respond by routing to /paywall.
  bool get chatLimitReached => _chatLimitReached;

  void clearChatLimitReached() {
    _chatLimitReached = false;
    safeNotifyListeners();
  }

  /// Null for main Buddy chat; the agent identifier string for agent threads.
  String? get agentId;

  /// The resolved, non-anonymous user ID set during [init]. Null if not yet
  /// initialized or the user is anonymous.
  String? get userId => _currentUserId;

  /// Exposed to subclasses for session bootstrapping.
  ChatRepository get chatRepository => _chatRepository;

  // Init 

  /// Called by the screen once the userId is known. Subclasses control which
  /// session gets loaded via [initializeSession].
  Future<void> init(String? userId) async {
    _currentUserId = _normalizeUserId(userId);

    await _loadSessions();
    if (agentId == null) {
      // Main chat: restore cloud history + repair unsynced rows 
      // (guarded so it runs once per uid; safe to re-run when auth resolves later).
      await ensureRestoredForUser(userId);
    }

    await initializeSession();
  }

  /// Restores cloud-backed history and repairs unsynced local rows for [userId],
  /// then drains the backup queue. Only the main Buddy chat (agentId == null) is
  /// backed up. Safe to call repeatedly — e.g. again once auth resolves after a
  /// fresh install, because the per-uid guard runs the work at most once.
  Future<void> ensureRestoredForUser(String? userId) async {
    final uid = _normalizeUserId(userId);
    if (uid == null || agentId != null) return;
    if (_restoreAttemptedForUid == uid) return;
    // Set the guard BEFORE the first await so two near-simultaneous callers
    // (the post-frame init and the auth-state listener) can't both pass it.
    _restoreAttemptedForUid = uid;
    _currentUserId = uid;

    final restored = await _chatBackupService.restoreFromBackupIfLocalEmpty(uid);
    if (restored) await _loadSessions();
    await _chatBackupService.backfillUnsynced(uid);
    unawaited(_chatBackupService.processPendingJobs(userId: uid));
    safeNotifyListeners();
  }

  /// Drains queued chat backups to Firestore. Called on app lifecycle changes so
  /// a message sent just before backgrounding isn't lost if the OS kills us.
  Future<void> flushPendingBackup(String? userId) async {
    final uid = _normalizeUserId(userId);
    if (uid == null) return;
    await _chatBackupService.processPendingJobs(userId: uid);
  }

  /// Opens the correct session on init. Default: reuse the most recent session
  /// if it is empty, otherwise create a fresh one (ChatGPT-style lifecycle).
  /// Subclasses may override for specialised loading (e.g. FCM tap).
  Future<void> initializeSession() async {
    final sessionId = await _sessionManager.getOrCreateFreshSession(
      userId: _currentUserId ?? '',
      agentId: agentId,
    );
    await _loadSession(sessionId);
  }

  // Session management

  Future<void> switchSession(String sessionId) async {
    if (_currentSessionId == sessionId) return;
    await _loadSession(sessionId);
  }

  Future<void> startNewChat() async {
    _streamSub?.cancel();
    _streamSub = null;
    _messages.clear();
    _isStreaming = false;
    _streamingOutput.value = StreamingSnapshot.empty;
    _error = null;
    // Reuse an existing empty session rather than creating a new one every time,
    // so the history list doesn't fill up with empty placeholder sessions.
    try {
      _currentSessionId = await _sessionManager.getOrCreateFreshSession(
        userId: _currentUserId ?? '',
        agentId: agentId,
      );
      _sessionTitleSet = false;
    } catch (e) {
      AppLogger.error('Failed to start new chat', error: e, tag: 'ChatViewModel');
    }
    _setState(ViewState.idle);
    await _refreshSessions();
  }

  //  Sending messages

  Future<void> sendMessage(
    String text,
    String userId, {
    List<ChatAttachment>? attachments,
  }) async {
    final trimmed = text.trim();
    final hasAttachments = attachments != null && attachments.isNotEmpty;
    if (trimmed.isEmpty && !hasAttachments) return;
    _currentUserId = _normalizeUserId(userId);

    // Any send consumes the curiosity pills — they only make sense before the
    // user has answered the follow-up.
    if (_threadSuggestions.isNotEmpty) {
      _threadSuggestions = const [];
    }

    final userMsg = ChatMessageModel(
      id: _uuid.v4(),
      text: trimmed,
      isUser: true,
      timestamp: DateTime.now(),
      channel: ChatMessageChannel.text,
      sessionId: _currentSessionId,
      attachments: attachments,
    );

    final saved = await _persistMessage(userMsg);
    if (!saved) return;

    // First reply in a notification-opened thread = the re-engagement payoff.
    // One mechanism fires it for every origin (signal action / thread reply /
    // icebreaker reply), so a new decider can never forget to wire its funnel
    // step. Fire once, then disarm so later replies don't double-count.
    final pendingReply = _pendingReply;
    if (pendingReply != null) {
      unawaited(postHogAnalytics.trackEvent(
        pendingReply.event,
        properties: pendingReply.properties,
      ));
      _pendingReply = null;
    }

    final notificationReason = _pendingNotificationReason;
    _pendingNotificationReason = null;

    _setState(ViewState.loading);

    if (!_sessionTitleSet && _currentSessionId != null) {
      _sessionTitleSet = true;
      // First message of this session means a new conversation just began. 
      // This branch only runs while no title is set yet, so it fires once per session.
      unawaited(postHogAnalytics.trackEvent(
        'chat_session_started',
        properties: {'agent_id': agentId ?? 'general'},
      ));

      final title = trimmed.length > 60 ? '${trimmed.substring(0, 57)}...' : trimmed;
      unawaited(_persistSessionTitle(_currentSessionId!, title));
    }

    _streamResponse(trimmed, userMsg, notificationReason: notificationReason);
  }

  Future<void> retryLastMessage(String errorMessageId) async {
    if (_currentUserId == null) return;

    final errorIndex = _messages.indexWhere((m) => m.id == errorMessageId);
    if (errorIndex < 0) return;
    if (_messages[errorIndex].status != MessageStatus.error) return;

    ChatMessageModel? userMsg;
    for (var i = errorIndex - 1; i >= 0; i--) {
      if (_messages[i].isUser) {
        userMsg = _messages[i];
        break;
      }
    }
    if (userMsg == null) return;

    unawaited(_chatRepository.deleteMessage(_messages[errorIndex].id));
    _messages.removeAt(errorIndex);
    _error = null;
    safeNotifyListeners();

    _setState(ViewState.loading);
    _streamResponse(userMsg.text, userMsg);
  }

  Future<void> editAndResend(String messageId, String newText) async {
    if (_currentUserId == null) return;
    final idx = _messages.indexWhere((m) => m.id == messageId);
    if (idx < 0 || !_messages[idx].isUser) return;

    final updated = _messages[idx].copyWith(text: newText);
    _messages[idx] = updated;
    if (idx + 1 < _messages.length) _messages.removeRange(idx + 1, _messages.length);
    safeNotifyListeners();

    await _chatRepository.updateMessageContent(messageId, newText);
    final seq = await _chatRepository.getMessageSequence(messageId);
    if (seq != null && _currentSessionId != null) {
      await _chatRepository.deleteMessagesAfter(_currentSessionId!, seq);
    }

    _setState(ViewState.loading);
    _streamResponse(newText, updated);
  }

  Future<void> submitClarification(
    String clarificationId,
    List<String> selectedOptions,
  ) async {
    if (_currentUserId == null || selectedOptions.isEmpty) return;

    final idx = _messages.indexWhere(
      (m) => m.clarificationPayload?.clarificationId == clarificationId,
    );
    if (idx >= 0) {
      final updated = _messages[idx].copyWith(
        clarificationPayload: () => _messages[idx]
            .clarificationPayload
            ?.copyWith(selectedOptions: () => selectedOptions),
      );
      _messages[idx] = updated;
      safeNotifyListeners();
      unawaited(_chatRepository.saveMessage(updated, userId: _currentUserId));
    }
    await sendMessage(selectedOptions.join(', '), _currentUserId!);
  }

  Future<void> setFeedback(String messageId, MessageFeedback? feedback) async {
    final idx = _messages.indexWhere((m) => m.id == messageId);
    if (idx < 0 || _messages[idx].isUser) return;

    _messages[idx] = _messages[idx].copyWith(feedback: () => feedback);
    safeNotifyListeners();
    await _chatRepository.updateFeedback(messageId, feedback);

    if (_currentUserId != null && _currentSessionId != null) {
      unawaited(_feedbackService.saveFeedback(
        userId: _currentUserId!,
        messageId: messageId,
        sessionId: _currentSessionId!,
        feedback: feedback,
        messageContent: _messages[idx].text,
      ));
    }
  }

  void clearError() {
    _error = null;
    _setState(_messages.isEmpty ? ViewState.idle : ViewState.loaded);
  }

  /// Cancels the active stream and discards any partial response.
  /// The user message is already persisted — only the incomplete assistant
  /// text is dropped. No-op if nothing is streaming.
  void stopGeneration() {
    if (!_isStreaming) return;
    _streamSub?.cancel();
    _streamSub = null;
    _isStreaming = false;
    _streamingOutput.value = StreamingSnapshot.empty;
    _setState(_messages.isEmpty ? ViewState.idle : ViewState.loaded);
  }

  // Curiosity follow-up (thread) pre-load

  /// Suggestion chips for an open curiosity follow-up. Empty once the user has
  /// replied (or for a thread that was already answered in the notification
  /// shade). The chat screen renders these above the input; tapping one calls
  /// [sendMessage].
  List<String> _threadSuggestions = const [];
  List<String> get threadSuggestions => List.unmodifiable(_threadSuggestions);

  /// Pre-loads a curiosity follow-up thread when its notification is tapped.
  ///
  /// If [priorMessages] is non-empty the user already answered in the shade, so
  /// we reconcile that server-side exchange into the chat and show no pills.
  /// Otherwise we seed Buddy's question as the opener and surface the pills.
  /// [priorMessages] entries are `{role, content}` maps from
  /// `BackendApiService.fetchThreadMessages`.
  Future<void> loadThreadFollowUpContext({
    required String threadId,
    required String question,
    required List<String> suggestedReplies,
    List<Map<String, dynamic>> priorMessages = const [],
    String notificationReason = '',
  }) async {
    _messages.clear();
    _error = null;
    _pendingNotificationReason = notificationReason.isEmpty ? null : notificationReason;

    if (priorMessages.isNotEmpty) {
      // Reconcile the shade exchange. Persist oldest-first so order is correct.
      for (final m in priorMessages) {
        final content = (m['content'] as String?)?.trim() ?? '';
        if (content.isEmpty) continue;
        await _persistMessage(ChatMessageModel(
          id: _uuid.v4(),
          text: content,
          isUser: (m['role'] as String?) == 'user',
          timestamp: DateTime.now(),
          channel: ChatMessageChannel.text,
          sessionId: _currentSessionId,
        ));
      }
      _threadSuggestions = const [];
    } else {
      if (question.isNotEmpty) {
        await _persistMessage(ChatMessageModel(
          id: _uuid.v4(),
          text: question,
          isUser: false,
          timestamp: DateTime.now(),
          channel: ChatMessageChannel.text,
          sessionId: _currentSessionId,
        ));
      }
      _threadSuggestions = List.unmodifiable(suggestedReplies);
    }

    _setState(ViewState.loaded);
    await _refreshSessions();

    // Funnel session step: the chat opened from a follow-up tap. Arm the action
    // event only when the user hasn't already answered in the shade (those
    // replies are counted server-side, so arming would double-count).
    _armNotificationFunnel(
      sessionEvent: FunnelEvents.threadSessionFromNotification,
      sessionProps: {
        FunnelEvents.propThreadId: threadId,
        FunnelEvents.propNotificationOrigin: FunnelEvents.originThreadEngine,
      },
      // Arm the in-chat reply only when the user has not already answered in the
      // shade (those are counted server-side; arming would double-count).
      replyEvent: priorMessages.isEmpty ? FunnelEvents.threadReply : null,
      replyProps: {
        FunnelEvents.propThreadId: threadId,
        FunnelEvents.propNotificationOrigin: FunnelEvents.originThreadEngine,
        'channel': 'in_chat',
      },
    );
  }

  // Engagement pre-load

  /// Pre-loads an assistant message from an engagement notification tap before
  /// the user types anything. Fires the responded callback in the background.
  Future<void> loadEngagementContext({
    required String engagementId,
    required String agentContext,
    required String initialMessage,
  }) async {
    _messages.clear();
    _error = null;
    await _openFreshSession();

    final msg = ChatMessageModel(
      id: _uuid.v4(),
      text: initialMessage,
      isUser: false,
      timestamp: DateTime.now(),
      channel: ChatMessageChannel.text,
      sessionId: _currentSessionId,
      engagementId: engagementId,
      engagementAgent: agentContext,
    );
    await _persistMessage(msg);
    _setState(ViewState.loaded);
    await _refreshSessions();
    if (engagementId.isNotEmpty) {
      unawaited(_backendService.markEngagementResponded(engagementId));
    }
  }

  /// Pre-loads the framed opener from a signal-engine content notification and
  /// arms funnel attribution: fires signal_session_from_notification now; the
  /// user's first reply will fire signal_action_after_notification (see
  /// [sendMessage]). Mirrors [loadEngagementContext] without the engagement id.
  Future<void> loadSignalNotificationContext({
    required String notificationId,
    required String contentId,
    required String category,
    required String initialMessage,
    String notificationReason = '',
  }) async {
    _messages.clear();
    _error = null;
    _pendingNotificationReason = notificationReason.isEmpty ? null : notificationReason;

    if (initialMessage.isNotEmpty) {
      final msg = ChatMessageModel(
        id: _uuid.v4(),
        text: initialMessage,
        isUser: false,
        timestamp: DateTime.now(),
        channel: ChatMessageChannel.text,
        sessionId: _currentSessionId,
      );
      await _persistMessage(msg);
    }
    _setState(ViewState.loaded);
    await _refreshSessions();

    _armNotificationFunnel(
      sessionEvent: FunnelEvents.sessionFromNotification,
      sessionProps: {
        FunnelEvents.propNotificationId: notificationId,
        FunnelEvents.propContentId: contentId,
        FunnelEvents.propCategory: category,
        FunnelEvents.propNotificationOrigin: FunnelEvents.originSignalEngine,
      },
      replyEvent: FunnelEvents.actionAfterNotification,
      replyProps: {
        FunnelEvents.propNotificationId: notificationId,
        FunnelEvents.propContentId: contentId,
        FunnelEvents.propCategory: category,
        FunnelEvents.propNotificationOrigin: FunnelEvents.originSignalEngine,
      },
    );
  }

  /// Fires the "chat opened from a notification" session funnel step, and when
  /// the origin has a first-reply step, arms it to fire exactly once on the
  /// user's next send. Every seeded-opener path funnels through here so session
  /// and reply attribution are wired identically for all proactive deciders —
  /// the unification that keeps a new decider from silently dropping its funnel.
  void _armNotificationFunnel({
    required String sessionEvent,
    required Map<String, Object> sessionProps,
    String? replyEvent,
    Map<String, Object> replyProps = const {},
  }) {
    unawaited(
      postHogAnalytics.trackEvent(sessionEvent, properties: sessionProps),
    );
    _pendingReply = replyEvent == null
        ? null
        : _PendingNotificationReply(replyEvent, replyProps);
  }

  /// Pre-loads the opener from an icebreaker notification tap and arms funnel
  /// attribution: fires icebreaker_session_from_notification now; the user's
  /// first reply fires icebreaker_reply once (see [sendMessage]). Mirrors
  /// [loadSignalNotificationContext].
  Future<void> loadIcebreakerContext({
    required String notificationId,
    required String openingMessage,
    String notificationReason = '',
  }) async {
    _messages.clear();
    _error = null;
    _pendingNotificationReason = notificationReason.isEmpty ? null : notificationReason;

    if (openingMessage.isNotEmpty) {
      final msg = ChatMessageModel(
        id: _uuid.v4(),
        text: openingMessage,
        isUser: false,
        timestamp: DateTime.now(),
        channel: ChatMessageChannel.text,
        sessionId: _currentSessionId,
      );
      await _persistMessage(msg);
    }
    _setState(ViewState.loaded);
    await _refreshSessions();

    _armNotificationFunnel(
      sessionEvent: FunnelEvents.icebreakerSessionFromNotification,
      sessionProps: {
        FunnelEvents.propNotificationId: notificationId,
        FunnelEvents.propNotificationOrigin: FunnelEvents.originIcebreaker,
      },
      replyEvent: FunnelEvents.icebreakerReply,
      replyProps: {
        FunnelEvents.propNotificationId: notificationId,
        FunnelEvents.propNotificationOrigin: FunnelEvents.originIcebreaker,
        'channel': 'in_chat',
      },
    );
  }

  /// Starts a NEW dedicated conversation about the daily briefing, embedded in the
  /// briefing screen. The briefing is seeded as the VISIBLE first assistant bubble
  /// [newsMessage] of a fresh session, so the user sees the news as message #1 and the
  /// whole exchange is self-contained in chat history. [firstUserMessage] is then sent
  /// as the next turn; because the news is now real history, no hidden context is
  /// needed: Buddy answers informed through [_buildHistory] (its synthetic "Hey Buddy."
  /// prefix covers the assistant-first turn). Arms `briefing_chat_started` once.
  Future<void> startBriefingConversation({
    required String newsMessage,
    required String firstUserMessage,
  }) async {
    final question = firstUserMessage.trim();
    if (question.isEmpty || _currentUserId == null) return;

    // Seed into a clean, empty session so the news is message[0] and we never append to
    // an unrelated recent chat. init() already opened an empty session on screen load;
    // only force a fresh one if something is somehow already loaded.
    if (_messages.isNotEmpty) {
      await _openFreshSession();
    }

    if (newsMessage.trim().isNotEmpty) {
      await _persistMessage(ChatMessageModel(
        id: _uuid.v4(),
        text: newsMessage,
        isUser: false,
        timestamp: DateTime.now(),
        channel: ChatMessageChannel.text,
        sessionId: _currentSessionId,
      ));
    }
    _setState(ViewState.loaded);
    await _refreshSessions();

    _pendingReply = _PendingNotificationReply(
      FunnelEvents.briefingChatStarted,
      {
        FunnelEvents.propNotificationOrigin: FunnelEvents.originBriefing,
        'channel': 'in_chat',
      },
    );

    await sendMessage(question, _currentUserId!);
  }

  /// Pre-loads the opener from a topic-tracker live-update notification tap. Seeds
  /// Buddy's update as the first bubble. v1 has no funnel attribution for trackers,
  /// so unlike the other notification origins this only seeds the opener.
  Future<void> loadTrackerContext({
    required String openingMessage,
  }) async {
    _messages.clear();
    _error = null;

    if (openingMessage.isNotEmpty) {
      final msg = ChatMessageModel(
        id: _uuid.v4(),
        text: openingMessage,
        isUser: false,
        timestamp: DateTime.now(),
        channel: ChatMessageChannel.text,
        sessionId: _currentSessionId,
      );
      await _persistMessage(msg);
    }
    _setState(ViewState.loaded);
    await _refreshSessions();
  }

  // Subclass hooks

  /// Inserts [msg] at the front of the in-memory message list without
  /// persisting to Drift. A subclass hook for surfacing a notification
  /// opener as the first visible bubble in a fresh thread.
  void insertEphemeralMessage(ChatMessageModel msg) {
    _messages.insert(0, msg);
    _state = ViewState.loaded;
    safeNotifyListeners();
  }

  // Private helpers

  void _streamResponse(
    String text,
    ChatMessageModel userMsg, {
    String? notificationReason,
  }) {
    _isStreaming = true;
    _streamingOutput.value = StreamingSnapshot.empty;
    _streamSub?.cancel();
    safeNotifyListeners();

    // Stable id for THIS turn's assistant reply, derived from the user message id (which
    // is also the backend's client_message_id). The live reply, a "finishing up" pending
    // placeholder, and the server-hydrated final reply all share this id, so each just
    // upserts over the last — no duplicate bubbles regardless of arrival order.
    final replyId = '${userMsg.id}::reply';
    // True once any event has arrived, i.e. the server accepted the stream and recorded a
    // turn it can finish in the background. A transport drop after this is recoverable
    // (pending), not a dead-end error.
    var streamStarted = false;
    // End-to-end turn latency: ttft is stamped on the first stream event (the
    // streamStarted flip), total on DoneEvent. Analytics only; the streaming
    // render path is untouched.
    final turnStopwatch = Stopwatch()..start();
    int? firstEventElapsedMs;

    _streamSub = _backendService
        .sendMessageStream(
          text,
          _currentUserId ?? '',
          history: _buildHistory(exclude: userMsg),
          sessionId: _currentSessionId,
          clientMessageId: userMsg.id,
          agentId: agentId,
          attachments: userMsg.attachments,
          notificationReason: notificationReason,
        )
        .listen(
      (event) {
        if (!streamStarted) {
          streamStarted = true;
          firstEventElapsedMs = turnStopwatch.elapsedMilliseconds;
        }
        switch (event) {
          case TextDeltaEvent(:final delta):
            // Per-token update on the notifier only — repaints the streaming
            // bubble, not the whole screen.
            final current = _streamingOutput.value;
            _streamingOutput.value = StreamingSnapshot(
              text: current.text + delta,
              thinkingMessage: current.thinkingMessage,
            );

          case ToolThinkingEvent(:final message):
            _streamingOutput.value = StreamingSnapshot(
              text: _streamingOutput.value.text,
              thinkingMessage: message,
            );

          case ClarificationUiEvent(
              :final clarificationId,
              :final question,
              :final options,
              :final multiSelect,
            ):
            _isStreaming = false;
            _streamingOutput.value = StreamingSnapshot.empty;
            final clarMsg = ChatMessageModel(
              id: _uuid.v4(),
              text: '',
              isUser: false,
              timestamp: DateTime.now(),
              channel: ChatMessageChannel.text,
              sessionId: _currentSessionId,
              clarificationPayload: ClarificationPayload(
                clarificationId: clarificationId,
                question: question,
                options: options,
                multiSelect: multiSelect,
              ),
            );
            unawaited(_persistMessage(clarMsg));
            _error = null;
            _setState(ViewState.loaded);

          case DoneEvent(:final metadata, :final awaitingClarification):
            if (awaitingClarification) return;
            _isStreaming = false;
            final reminderJson = metadata?['reminder'] as Map<String, dynamic>?;
            final assistantMsg = ChatMessageModel(
              id: replyId,
              text: _streamingOutput.value.text,
              isUser: false,
              timestamp: DateTime.now(),
              channel: ChatMessageChannel.text,
              sessionId: _currentSessionId,
              reminderPayload:
                  reminderJson != null ? ReminderPayload.fromJson(reminderJson) : null,
            );
            _streamingOutput.value = StreamingSnapshot.empty;
            unawaited(_persistMessage(assistantMsg));
            _error = null;
            _setState(ViewState.loaded);
            ErrorHandler.logBreadcrumb('message_sent');
            unawaited(AnalyticsService.logMessageSent(agentId ?? 'general'));
            unawaited(postHogAnalytics.trackEvent(
              'chat_message_sent',
              properties: {'agent_type': agentId ?? 'general'},
            ));
            unawaited(postHogAnalytics.trackEvent(
              'chat_e2e_latency',
              properties: {
                'ttft_ms': firstEventElapsedMs ?? turnStopwatch.elapsedMilliseconds,
                'total_ms': turnStopwatch.elapsedMilliseconds,
                'agent_type': agentId ?? 'general',
              },
            ));

          case ChatLimitReachedEvent():
            _isStreaming = false;
            _streamingOutput.value = StreamingSnapshot.empty;
            _chatLimitReached = true;
            _setState(_messages.isEmpty ? ViewState.idle : ViewState.loaded);

          case ErrorStreamEvent(:final message, :final code):
            _isStreaming = false;
            _streamingOutput.value = StreamingSnapshot.empty;

            // A server-emitted error event (code == null) carries copy the backend
            // already wrote in Buddy's voice for this exact failure - show it verbatim. 
            // A client-side transport failure carries an ErrorCode we map.
            final reason = code == null
                ? (message.trim().isNotEmpty
                    ? message.trim()
                    : 'Something went wrong. Try again in a moment.')
                : _friendlyError(AppException(code: code, message: message));
            final errMsg = ChatMessageModel(
              id: replyId,
              text: '',
              isUser: false,
              timestamp: DateTime.now(),
              channel: ChatMessageChannel.text,
              sessionId: _currentSessionId,
              status: MessageStatus.error,
              errorReason: reason,
            );
            unawaited(_persistMessage(errMsg));
            _setState(ViewState.loaded);
            AppLogger.warning('Stream error: $message', tag: 'ChatViewModel');
        }
      },
      onError: (Object e, StackTrace st) {
        _isStreaming = false;
        final partial = _streamingOutput.value.text;
        _streamingOutput.value = StreamingSnapshot.empty;

        // A transport drop AFTER the server accepted the stream (we saw events, or have
        // partial text) means the turn is finishing server-side. Show a calm "finishing
        // up" state, keep whatever streamed, and let the push + hydration deliver the
        // rest — never the misleading "check your connection". Only a failure with
        // nothing received (the request likely never reached the server, so no turn was
        // recorded) is a real dead-end the user should retry.
        final recoverable = streamStarted || partial.trim().isNotEmpty;
        if (recoverable) {
          final pendingMsg = ChatMessageModel(
            id: replyId,
            text: partial,
            isUser: false,
            timestamp: DateTime.now(),
            channel: ChatMessageChannel.text,
            sessionId: _currentSessionId,
            status: MessageStatus.pending,
          );
          unawaited(_persistMessage(pendingMsg));
          _setState(ViewState.loaded);
          AppLogger.info(
            'Stream dropped mid-turn; awaiting server completion',
            tag: 'ChatViewModel',
          );
          return;
        }

        ErrorHandler.handle(e, st);
        final exc = e is AppException
            ? e
            : AppException.unexpected(e.toString(), error: e, stackTrace: st);
        // Bubble only, no banner.
        final errMsg = ChatMessageModel(
          id: replyId,
          text: '',
          isUser: false,
          timestamp: DateTime.now(),
          channel: ChatMessageChannel.text,
          sessionId: _currentSessionId,
          status: MessageStatus.error,
          errorReason: _friendlyError(exc),
        );
        unawaited(_persistMessage(errMsg));
        _setState(ViewState.loaded);
      },
    );
  }

  Future<void> _loadSession(String sessionId) async {
    _streamSub?.cancel();
    _streamSub = null;
    _currentSessionId = sessionId;
    _messages.clear();
    _isStreaming = false;
    _streamingOutput.value = StreamingSnapshot.empty;

    ChatSession? session;
    for (final s in _sessions) {
      if (s.id == sessionId) {
        session = s;
        break;
      }
    }
    _sessionTitleSet = session?.title != null;

    // If Drift has no messages for this session, attempt a Firestore restore before
    // loading. Covers: fresh install, cache cleared, or any other Drift data loss.
    // Skipped when messages already exist locally so normal sessions have no extra cost.
    if (_currentUserId != null) {
      await _chatBackupService.restoreSessionMessagesIfEmpty(
        _currentUserId!,
        sessionId,
      );
    }

    final result = await _chatRepository.loadMessages(sessionId);
    result.when(
      success: (msgs) {
        _messages
          ..clear()
          ..addAll(msgs);
        _state = _messages.isEmpty ? ViewState.idle : ViewState.loaded;
        _error = null;
        safeNotifyListeners();
      },
      failure: (e) {
        AppLogger.error('Failed to load messages', error: e, tag: 'ChatViewModel');
        _state = ViewState.error;
        _error = e;
        safeNotifyListeners();
      },
    );
  }

  Future<void> _openFreshSession({String? withAgentId}) async {
    try {
      _currentSessionId = await _chatRepository.createSession(
        userId: _currentUserId ?? '',
        agentId: withAgentId ?? agentId,
      );
      _sessionTitleSet = false;
      await _refreshSessions();
    } catch (e) {
      AppLogger.error('Failed to create session', error: e, tag: 'ChatViewModel');
    }
  }

  Future<void> _refreshSessions() async => _loadSessions(notify: true);

  Future<void> _loadSessions({bool notify = false}) async {
    final result = await _chatRepository.getSessionsForAgent(
      userId: _currentUserId ?? '',
      agentId: agentId,
    );
    result.when(
      success: (sessions) {
        _sessions = sessions;
        if (notify) safeNotifyListeners();
      },
      failure: (e) {
        AppLogger.error('Failed to load sessions', error: e, tag: 'ChatViewModel');
      },
    );
  }

  Future<bool> _persistMessage(ChatMessageModel msg) async {
    // Upsert by id: a message with an existing id REPLACES it in place rather than
    // appending a duplicate. Most callers pass a fresh uuid (so this just appends), but
    // the assistant reply uses a stable id (`<cmid>::reply`) so a "finishing up"
    // placeholder is cleanly replaced by the hydrated final reply, in order.
    final existingIdx = _messages.indexWhere((m) => m.id == msg.id);
    final ChatMessageModel? previous = existingIdx >= 0 ? _messages[existingIdx] : null;
    if (existingIdx >= 0) {
      _messages[existingIdx] = msg;
    } else {
      _messages.add(msg);
    }
    safeNotifyListeners();

    final result = await _chatRepository.saveMessage(msg, userId: _currentUserId);
    if (result.isFailure) {
      // Roll back this id to its prior state (restore the replaced message, or remove
      // the one we appended).
      final idx = _messages.indexWhere((m) => m.id == msg.id);
      if (idx >= 0) {
        if (previous != null) {
          _messages[idx] = previous;
        } else {
          _messages.removeAt(idx);
        }
      }
      _error = result.errorOrNull ??
          AppException.unexpected('Failed to save message locally.');
      _state = ViewState.error;
      safeNotifyListeners();
      return false;
    }
    return true;
  }

  /// Pull a reply that finished server-side while the app was backgrounded and merge it
  /// into the chat, replacing the "finishing up" placeholder (or adding it if the live
  /// session lost it). Safe to call repeatedly: the stable `<cmid>::reply` id upserts.
  Future<void> hydrateServerReply(String clientMessageId) async {
    final uid = _currentUserId;
    if (uid == null || uid.isEmpty || clientMessageId.isEmpty) return;

    final reply = await _chatBackupService.fetchServerReply(uid, clientMessageId);
    if (reply == null) return;

    final replyId = '$clientMessageId::reply';
    if (reply.status == 'complete' && reply.answerText.trim().isNotEmpty) {
      await _persistMessage(ChatMessageModel(
        id: replyId,
        text: reply.answerText,
        isUser: false,
        timestamp: DateTime.now(),
        channel: ChatMessageChannel.text,
        sessionId: _currentSessionId ??
            (reply.sessionId.isNotEmpty ? reply.sessionId : null),
        reminderPayload:
            reply.reminder != null ? ReminderPayload.fromJson(reply.reminder!) : null,
      ));
      _setState(ViewState.loaded);
    } else if (reply.status == 'failed') {
      // Background completion gave up: turn the placeholder into an honest, retryable error.
      await _persistMessage(ChatMessageModel(
        id: replyId,
        text: '',
        isUser: false,
        timestamp: DateTime.now(),
        channel: ChatMessageChannel.text,
        sessionId: _currentSessionId,
        status: MessageStatus.error,
        errorReason: "I couldn't finish that one. Tap retry to give it another go.",
      ));
      _setState(ViewState.loaded);
    }
    // Otherwise still generating/regenerating: leave the placeholder for a later sweep.
  }

  /// On resume / session open, finish hydrating any replies that completed in the
  /// background. Scans the loaded session for "finishing up" placeholders and pulls each.
  Future<void> reconcilePendingTurns() async {
    const suffix = '::reply';
    final pendingClientMessageIds = _messages
        .where((m) =>
            !m.isUser &&
            m.status == MessageStatus.pending &&
            m.id.endsWith(suffix))
        .map((m) => m.id.substring(0, m.id.length - suffix.length))
        .toList();
    for (final cmid in pendingClientMessageIds) {
      await hydrateServerReply(cmid);
    }
  }

  Future<void> _persistSessionTitle(String sessionId, String title) async {
    final result = await _chatRepository.setSessionTitle(
      sessionId,
      title,
      userId: _currentUserId,
    );
    result.when(
      success: (_) => unawaited(_refreshSessions()),
      failure: (e) => AppLogger.error(
        'Failed to set session title',
        error: e,
        tag: 'ChatViewModel',
      ),
    );
  }

  Future<void> _primeConnectivityState() async {
    _isOffline = !await _connectivityService.isConnected;
    safeNotifyListeners();
  }

  void _setState(ViewState next) {
    _state = next;
    safeNotifyListeners();
  }

  static const _multimodalHistoryTurns = 3;

  List<Map<String, dynamic>> _buildHistory({ChatMessageModel? exclude}) {
    final window = AppConstants.chatHistoryWindow;
    final source = _messages
        .where((m) => m != exclude && m.status != MessageStatus.error)
        .toList();
    final slice =
        source.length > window ? source.sublist(source.length - window) : source;

    final turns = <Map<String, dynamic>>[];
    for (var i = 0; i < slice.length; i++) {
      final isRecent = i >= slice.length - _multimodalHistoryTurns;
      turns.add(slice[i].toHistoryTurn(includeAttachments: isRecent));
    }

    if (turns.isNotEmpty && turns.first['role'] == 'assistant') {
      turns.insert(0, {'role': 'user', 'content': 'Hey Buddy.'});
    }
    return turns;
  }

  /// Maps a client-side transport/HTTP failure to user copy by its [ErrorCode].
  /// Server-emitted error events are shown verbatim at the call site (the backend
  /// owns that copy), so this only ever sees a code-bearing transport failure or an
  /// `unexpected` wrapper — there is no message-substring guessing here, which is
  /// what used to let a timeout read as a "check your connection" error.
  static String _friendlyError(AppException error) {
    switch (error.code) {
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
        return error.message; // context-specific copy set at the call site
      case ErrorCode.forbidden:
      case ErrorCode.notFound:
      case ErrorCode.firestoreReadFailed:
      case ErrorCode.firestoreWriteFailed:
      case ErrorCode.documentNotFound:
      case ErrorCode.unexpected:
      case ErrorCode.unknown:
        return 'Something went wrong. Try again in a moment.';
    }
  }

  static String? _normalizeUserId(String? id) {
    if (id == null) return null;
    final t = id.trim();
    return (t.isEmpty || t == 'anonymous') ? null : t;
  }

  @override
  void dispose() {
    _connectivitySub?.cancel();
    _streamSub?.cancel();
    _streamingOutput.dispose();
    super.dispose();
  }
}
