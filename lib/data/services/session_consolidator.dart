import '../../core/logging/app_logger.dart';
import '../models/chat_message_model.dart';
import 'backend_api_service.dart';

/// Ships a finished chat session's transcript to the per-session reflection tier (the
/// narrative layer of UserAura) when the app goes to the background.
///
/// Why this shape: the chat is client-owned (the local drift DB), so the server can't
/// reflect on a session unless the client sends it. The server is idempotent per session
/// id and consent-gated, so this is safe to fire-and-forget and to retry.
///
/// Mirrors [BuddyPillsRefresher]: cheap when nothing happened (a session with fewer than
/// two user turns is skipped), can't spam (a session is re-sent only if it GREW since the
/// last successful send), and a failed send is never marked done so the next background
/// retries it. The only state held is a per-session "last sent size" dedupe map, which is
/// app-session lifecycle state, not per-request transient state.
class SessionConsolidator {
  final BackendApiService _backendApiService;

  // sessionId -> turn count last successfully sent. A growing session consolidates again
  // as it gains turns; an unchanged one is skipped on a background->foreground bounce.
  final Map<String, int> _sentTurnCounts = {};

  SessionConsolidator({required BackendApiService backendApiService})
      : _backendApiService = backendApiService;

  /// Reflect on [messages] for [sessionId]. No-op unless there are at least two user
  /// turns and the session has grown since the last successful send. Never throws.
  Future<void> consolidate({
    required String? uid,
    required String? sessionId,
    required List<ChatMessageModel> messages,
  }) async {
    if (uid == null || uid.isEmpty || sessionId == null || sessionId.isEmpty) return;

    final turns = messages
        .where((m) => m.text.trim().isNotEmpty)
        .map((m) => {'role': m.isUser ? 'user' : 'assistant', 'text': m.text})
        .toList();

    final userTurns = turns.where((t) => t['role'] == 'user').length;
    if (userTurns < 2) return; // no arc worth reflecting on

    if (_sentTurnCounts[sessionId] == turns.length) return; // already sent at this size

    final result = await _backendApiService.consolidateSession(
      sessionId: sessionId,
      turns: turns,
    );
    result.when(
      success: (_) {
        _sentTurnCounts[sessionId] = turns.length;
        AppLogger.info(
          'Session consolidation requested',
          tag: 'SessionConsolidator',
          metadata: {'sessionId': sessionId, 'turns': turns.length},
        );
      },
      // Not recorded as sent -> the next background trigger retries it.
      failure: (e) => AppLogger.warning(
        'Session consolidation failed (non-blocking, will retry)',
        tag: 'SessionConsolidator',
        metadata: {'error': e.message},
      ),
    );
  }
}
