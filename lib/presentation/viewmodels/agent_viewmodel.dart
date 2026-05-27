import 'dart:async';

import '../../core/logging/app_logger.dart';
import '../../data/models/chat_message_model.dart';
import '../../data/repositories/agent_suggestion_pills_repository.dart';
import 'chat_viewmodel.dart';

/// ViewModel for a per-agent chat thread.
/// Uses the base session lifecycle: reuse the most recent session if empty,
/// otherwise open a fresh one (same rule as main chat). Adds suggestion pills
/// on top of the shared chat behaviour.
class AgentViewModel extends ChatViewModel {
  final String _agentId;
  final AgentSuggestionPillsRepository _suggestionPillsRepository;

  List<String> _suggestionPills = const [];
  String? _pendingNudgeOpener;

  AgentViewModel({
    required String agentId,
    required super.backendService,
    required super.connectivityService,
    required super.chatRepository,
    required super.chatBackupService,
    required super.feedbackService,
    required super.chatSessionManager,
    required super.postHogAnalyticsService,
    required AgentSuggestionPillsRepository suggestionPillsRepository,
  })  : _agentId = agentId,
        _suggestionPillsRepository = suggestionPillsRepository;

  @override
  String get agentId => _agentId;

  List<String> get suggestionPills => _suggestionPills;

  /// Initializes the session. If [nudgeOpener] is provided and the session
  /// has no persisted messages, it is shown as the first assistant bubble.
  @override
  Future<void> init(String? userId, {String? nudgeOpener}) async {
    _pendingNudgeOpener = nudgeOpener;
    await super.init(userId);
  }

  @override
  Future<void> initializeSession() async {
    await super.initializeSession();
    unawaited(_fetchAndLoadSuggestionPills());
    _maybeInjectNudgeOpener();
    unawaited(postHogAnalytics.trackEvent(
      'agent_thread_opened',
      properties: {'agent_type': _agentId},
    ));
  }

  void _maybeInjectNudgeOpener() {
    final opener = _pendingNudgeOpener;
    if (opener == null || opener.isEmpty || messages.isNotEmpty) return;
    _pendingNudgeOpener = null;
    insertEphemeralMessage(ChatMessageModel(
      id: 'nudge_${DateTime.now().microsecondsSinceEpoch}',
      text: opener,
      isUser: false,
      timestamp: DateTime.now(),
      channel: ChatMessageChannel.text,
      sessionId: currentSessionId,
    ));
  }

  Future<void> _fetchAndLoadSuggestionPills() async {
    final uid = userId;
    if (uid == null) return;
    try {
      final pills = await _suggestionPillsRepository
          .fetchSuggestionPillsForAgent(uid, _agentId);
      if (pills.isEmpty) return;
      _suggestionPills = pills;
      safeNotifyListeners();
    } catch (e) {
      AppLogger.error(
        'Failed to load suggestion pills',
        error: e,
        tag: 'AgentViewModel',
        metadata: {'agentId': _agentId},
      );
    }
  }
}
