import 'dart:async';

import 'package:uuid/uuid.dart';

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
import '../../data/repositories/chat_repository.dart';
import '../../data/services/analytics_service.dart';
import '../../data/services/backend_api_service.dart';
import '../../data/services/chat_backup_service.dart';
import '../../data/services/chat_service_provider.dart';
import '../../data/services/chat_session_manager.dart';
import '../../data/services/feedback_service.dart';
import 'view_state.dart';

export 'view_state.dart';

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
  final _uuid = const Uuid();

  StreamSubscription<ConnectivityStatus>? _connectivitySub;
  StreamSubscription<ChatStreamEvent>? _streamSub;

  ViewState _state = ViewState.idle;
  AppException? _error;
  final List<ChatMessageModel> _messages = [];
  List<ChatSession> _sessions = const [];
  bool _isOffline = false;
  bool _isStreaming = false;
  String _streamingText = '';
  String? _thinkingMessage;
  String? _currentSessionId;
  String? _currentUserId;
  bool _sessionTitleSet = false;
  bool _chatLimitReached = false;

  ChatViewModel({
    required ChatServiceProvider backendService,
    required ConnectivityService connectivityService,
    required ChatRepository chatRepository,
    required ChatBackupService chatBackupService,
    required FeedbackService feedbackService,
    required ChatSessionManager chatSessionManager,
  })  : _backendService = backendService,
        _connectivityService = connectivityService,
        _chatRepository = chatRepository,
        _chatBackupService = chatBackupService,
        _feedbackService = feedbackService,
        _sessionManager = chatSessionManager {
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
  String get streamingText => _streamingText;
  String? get thinkingMessage => _thinkingMessage;
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

    if (agentId == null) {
      // Main chat: restore from backup on first launch, then process pending jobs
      await _loadSessions();
      if (_sessions.isEmpty && _currentUserId != null) {
        await _chatBackupService.restoreFromBackupIfLocalEmpty(_currentUserId!);
        await _loadSessions();
      }
      if (_currentUserId != null) {
        unawaited(_chatBackupService.processPendingJobs(userId: _currentUserId));
      }
    } else {
      // Agent threads: load session list so _sessionTitleSet resolves correctly
      // in _loadSession when the session already has a title.
      await _loadSessions();
    }

    await initializeSession();
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
    _streamingText = '';
    _thinkingMessage = null;
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

    _setState(ViewState.loading);

    if (!_sessionTitleSet && _currentSessionId != null) {
      _sessionTitleSet = true;
      final title = trimmed.length > 60 ? '${trimmed.substring(0, 57)}...' : trimmed;
      unawaited(_persistSessionTitle(_currentSessionId!, title));
    }

    _streamResponse(trimmed, userMsg);
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
    _streamingText = '';
    _thinkingMessage = null;
    _setState(_messages.isEmpty ? ViewState.idle : ViewState.loaded);
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

  // Subclass hooks

  /// Inserts [msg] at the front of the in-memory message list without
  /// persisting to Drift. Used by AgentViewModel to surface a notification
  /// nudge opener as the first visible bubble in a fresh thread.
  void insertEphemeralMessage(ChatMessageModel msg) {
    _messages.insert(0, msg);
    _state = ViewState.loaded;
    safeNotifyListeners();
  }

  // Private helpers

  void _streamResponse(String text, ChatMessageModel userMsg) {
    _isStreaming = true;
    _streamingText = '';
    _thinkingMessage = null;
    _streamSub?.cancel();

    _streamSub = _backendService
        .sendMessageStream(
          text,
          _currentUserId ?? '',
          history: _buildHistory(exclude: userMsg),
          sessionId: _currentSessionId,
          clientMessageId: userMsg.id,
          agentId: agentId,
          attachments: userMsg.attachments,
        )
        .listen(
      (event) {
        switch (event) {
          case TextDeltaEvent(:final delta):
            _streamingText += delta;
            safeNotifyListeners();

          case ToolThinkingEvent(:final message):
            _thinkingMessage = message;
            safeNotifyListeners();

          case ClarificationUiEvent(
              :final clarificationId,
              :final question,
              :final options,
              :final multiSelect,
            ):
            _isStreaming = false;
            _streamingText = '';
            _thinkingMessage = null;
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
              id: _uuid.v4(),
              text: _streamingText,
              isUser: false,
              timestamp: DateTime.now(),
              channel: ChatMessageChannel.text,
              sessionId: _currentSessionId,
              reminderPayload:
                  reminderJson != null ? ReminderPayload.fromJson(reminderJson) : null,
            );
            _streamingText = '';
            _thinkingMessage = null;
            unawaited(_persistMessage(assistantMsg));
            _error = null;
            _setState(ViewState.loaded);
            ErrorHandler.logBreadcrumb('message_sent');
            unawaited(AnalyticsService.logMessageSent(agentId ?? 'general'));

          case ChatLimitReachedEvent():
            _isStreaming = false;
            _streamingText = '';
            _thinkingMessage = null;
            _chatLimitReached = true;
            _setState(_messages.isEmpty ? ViewState.idle : ViewState.loaded);

          case ErrorStreamEvent(:final message):
            _isStreaming = false;
            _streamingText = '';
            _thinkingMessage = null;
            final exc = AppException.unexpected(message);
            final errMsg = ChatMessageModel(
              id: _uuid.v4(),
              text: '',
              isUser: false,
              timestamp: DateTime.now(),
              channel: ChatMessageChannel.text,
              sessionId: _currentSessionId,
              status: MessageStatus.error,
              errorReason: _friendlyError(exc),
            );
            unawaited(_persistMessage(errMsg));
            _error = exc;
            _setState(ViewState.error);
            AppLogger.warning('Stream error: $message', tag: 'ChatViewModel');
        }
      },
      onError: (Object e, StackTrace st) {
        ErrorHandler.handle(e, st);
        _isStreaming = false;
        _streamingText = '';
        _thinkingMessage = null;
        final exc = AppException.unexpected(e.toString(), error: e, stackTrace: st);
        final errMsg = ChatMessageModel(
          id: _uuid.v4(),
          text: '',
          isUser: false,
          timestamp: DateTime.now(),
          channel: ChatMessageChannel.text,
          sessionId: _currentSessionId,
          status: MessageStatus.error,
          errorReason: _friendlyError(exc),
        );
        unawaited(_persistMessage(errMsg));
        _error = exc;
        _setState(ViewState.error);
      },
    );
  }

  Future<void> _loadSession(String sessionId) async {
    _streamSub?.cancel();
    _streamSub = null;
    _currentSessionId = sessionId;
    _messages.clear();
    _isStreaming = false;
    _streamingText = '';
    _thinkingMessage = null;

    ChatSession? session;
    for (final s in _sessions) {
      if (s.id == sessionId) {
        session = s;
        break;
      }
    }
    _sessionTitleSet = session?.title != null;

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
    _messages.add(msg);
    safeNotifyListeners();

    final result = await _chatRepository.saveMessage(msg, userId: _currentUserId);
    if (result.isFailure) {
      _messages.remove(msg);
      _error = result.errorOrNull ??
          AppException.unexpected('Failed to save message locally.');
      _state = ViewState.error;
      safeNotifyListeners();
      return false;
    }
    return true;
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

  static String _friendlyError(AppException error) {
    final msg = error.message.toLowerCase();
    if (msg.contains('overloaded') || msg.contains('529')) {
      return "Your Buddy is busy right now. Try again in a moment.";
    }
    if (msg.contains('timeout') || msg.contains('timed out') ||
        msg.contains('network') || msg.contains('connection')) {
      return "Couldn't reach your Buddy. Check your connection and try again.";
    }
    if (msg.contains('rate limit') || msg.contains('429')) {
      return "You've hit your daily message limit. Upgrade to Pro for unlimited chat.";
    }
    return 'Something went wrong. Try again in a moment.';
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
    super.dispose();
  }
}
