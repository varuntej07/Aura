import 'dart:async';

import '../../core/logging/app_logger.dart';
import '../../data/models/chat_attachment.dart';
import '../../data/repositories/agent_suggestion_pills_repository.dart';
import '../../data/services/buddy_pills_refresher.dart';
import 'chat_viewmodel.dart';

/// ViewModel for the main Buddy text-chat screen opened from the drawer.
/// On a normal app open, delegates to the base session lifecycle (reuse empty
/// or create fresh). Overrides only for the FCM engagement tap path where a
/// specific session ID is pre-selected.
///
/// Adds personalized suggestion pills (grounded in the user's recent activity)
/// shown in the empty state, mirroring the per-agent pill behaviour but reading
/// the "buddy" key. Every send marks session activity so the pills regenerate
/// when the user next leaves the app.
class TextChatViewModel extends ChatViewModel {
  final String? initialSessionId;
  final AgentSuggestionPillsRepository _suggestionPillsRepository;
  final BuddyPillsRefresher _buddyPillsRefresher;

  List<String> _suggestionPills = const [];

  TextChatViewModel({
    this.initialSessionId,
    required super.backendService,
    required super.connectivityService,
    required super.chatRepository,
    required super.chatBackupService,
    required super.feedbackService,
    required super.chatSessionManager,
    required super.postHogAnalyticsService,
    required AgentSuggestionPillsRepository suggestionPillsRepository,
    required BuddyPillsRefresher buddyPillsRefresher,
  })  : _suggestionPillsRepository = suggestionPillsRepository,
        _buddyPillsRefresher = buddyPillsRefresher;

  @override
  String? get agentId => null;

  /// Personalized starter pills for the empty Buddy chat. Empty until loaded;
  /// the repository always returns a non-empty list (falls back to defaults).
  List<String> get suggestionPills => _suggestionPills;

  @override
  Future<void> initializeSession() async {
    if (initialSessionId != null) {
      await switchSession(initialSessionId!);
    } else {
      await super.initializeSession();
    }
    unawaited(_loadSuggestionPills());
  }

  Future<void> _loadSuggestionPills() async {
    final uid = userId;
    if (uid == null) return;
    try {
      final pills = await _suggestionPillsRepository
          .fetchSuggestionPillsForAgent(uid, 'buddy');
      if (pills.isEmpty) return;
      _suggestionPills = pills;
      safeNotifyListeners();
    } catch (e) {
      AppLogger.error(
        'Failed to load Buddy suggestion pills',
        error: e,
        tag: 'TextChatViewModel',
      );
    }
  }

  @override
  Future<void> sendMessage(
    String text,
    String userId, {
    List<ChatAttachment>? attachments,
  }) {
    // A real text turn, ground the next pill regeneration on it.
    _buddyPillsRefresher.markActivity();
    return super.sendMessage(
      text,
      userId,
      attachments: attachments,
    );
  }
}
