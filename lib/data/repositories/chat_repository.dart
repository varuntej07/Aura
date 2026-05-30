import 'dart:async';

import 'package:drift/drift.dart';
import 'package:uuid/uuid.dart';

import '../../core/errors/app_exception.dart';
import '../../core/network/api_response.dart';
import '../local/app_database.dart';
import '../models/chat_message_model.dart';
import '../models/clarification_payload.dart';
import '../services/chat_backup_service.dart';
import '../services/backend_api_service.dart';

class ChatRepository {
  final AppDatabase _db;
  final ChatBackupService _chatBackupService;

  static const _uuid = Uuid();

  ChatRepository({
    required AppDatabase db,
    required ChatBackupService chatBackupService,
  })  : _db = db,
        _chatBackupService = chatBackupService;

  /// Returns recent main Buddy chat sessions (no agent) for the given user.
  Future<Result<List<ChatSession>>> loadMainSessions({
    required String userId,
    int limit = 25,
    int offset = 0,
  }) async {
    try {
      final rows = await (_db.select(_db.chatSessions)
            ..where((t) => t.userId.equals(userId))
            ..where((t) => t.agentId.isNull())
            ..where((t) => t.messageCount.isBiggerThanValue(0))
            ..orderBy([(t) => OrderingTerm.desc(t.updatedAt)])
            ..limit(limit, offset: offset))
          .get();
      return Result.success(rows);
    } catch (e, st) {
      return Result.failure(
        AppException.unexpected('Failed to load sessions', error: e, stackTrace: st),
      );
    }
  }

  /// Returns the most recent session for the given user + agentId (null = main chat).
  Future<ChatSession?> getMostRecentSessionForAgent({
    required String userId,
    required String? agentId,
  }) async {
    final query = _db.select(_db.chatSessions);
    query.where((t) => t.userId.equals(userId));
    if (agentId == null) {
      query.where((t) => t.agentId.isNull());
    } else {
      query.where((t) => t.agentId.equals(agentId));
    }
    query
      ..orderBy([(t) => OrderingTerm.desc(t.updatedAt)])
      ..limit(1);
    return query.getSingleOrNull();
  }

  /// Returns all sessions for the given user + agentId (null = main chat), newest first.
  Future<Result<List<ChatSession>>> getSessionsForAgent({
    required String userId,
    required String? agentId,
  }) async {
    try {
      final query = _db.select(_db.chatSessions);
      query.where((t) => t.userId.equals(userId));
      if (agentId == null) {
        query.where((t) => t.agentId.isNull());
      } else {
        query.where((t) => t.agentId.equals(agentId));
      }
      query.where((t) => t.messageCount.isBiggerThanValue(0));
      query.orderBy([(t) => OrderingTerm.desc(t.updatedAt)]);
      final rows = await query.get();
      return Result.success(rows);
    } catch (e, st) {
      return Result.failure(
        AppException.unexpected('Failed to load sessions', error: e, stackTrace: st),
      );
    }
  }

  /// Returns the single persistent session for a user+agent, creating it if absent.
  Future<String> getOrCreateAgentSession({
    required String userId,
    required String agentId,
  }) async {
    final existing = await (_db.select(_db.chatSessions)
          ..where((t) => t.userId.equals(userId))
          ..where((t) => t.agentId.equals(agentId))
          ..limit(1))
        .getSingleOrNull();
    if (existing != null) return existing.id;
    return createSession(userId: userId, agentId: agentId);
  }

  Future<String> createSession({required String userId, String? agentId}) async {
    final id = _uuid.v4();
    final now = DateTime.now();
    await _db.into(_db.chatSessions).insert(
          ChatSessionsCompanion.insert(
            id: id,
            userId: Value(userId),
            startedAt: now,
            updatedAt: Value(now),
            agentId: Value(agentId),
          ),
        );
    return id;
  }

  Future<Result<List<ChatSession>>> loadRecentSessions({int limit = 10}) async {
    try {
      final rows = await (_db.select(_db.chatSessions)
            ..orderBy([(t) => OrderingTerm.desc(t.updatedAt)])
            ..limit(limit))
          .get();
      return Result.success(rows);
    } catch (e, st) {
      return Result.failure(
        AppException.unexpected(
          'Failed to load sessions',
          error: e,
          stackTrace: st,
        ),
      );
    }
  }

  Future<Result<void>> deleteSession(String sessionId) async {
    try {
      await (_db.delete(_db.chatSessions)..where((t) => t.id.equals(sessionId))).go();
      return const Result.success(null);
    } catch (e, st) {
      return Result.failure(
        AppException.unexpected(
          'Failed to delete session',
          error: e,
          stackTrace: st,
        ),
      );
    }
  }

  Future<Result<void>> saveMessage(
    ChatMessageModel msg, {
    String? userId,
  }) async {
    final sessionId = msg.sessionId;
    if (sessionId == null) {
      return Result.failure(
        AppException.unexpected('Failed to persist message: missing session id.'),
      );
    }

    try {
      await _db.transaction(() async {
        final session = await (_db.select(_db.chatSessions)
              ..where((t) => t.id.equals(sessionId)))
            .getSingleOrNull();
        if (session == null) {
          throw StateError('Chat session $sessionId not found');
        }

        final nextSequence = session.messageCount + 1;
        await _db.into(_db.chatMessages).insertOnConflictUpdate(
              ChatMessagesCompanion.insert(
                id: msg.id,
                sessionId: sessionId,
                content: msg.text,
                isUser: msg.isUser,
                channel: msg.channel.name,
                timestamp: msg.timestamp,
                sequence: Value(nextSequence),
                status: Value(msg.status.name),
                feedback: Value(msg.feedback?.name),
                errorReason: Value(msg.errorReason),
                engagementId: Value(msg.engagementId),
                engagementAgent: Value(msg.engagementAgent),
                reminderJson: Value(msg.reminderPayload?.toJsonString()),
                clarificationJson:
                    Value(msg.clarificationPayload?.toJsonString()),
              ),
            );

        await (_db.update(_db.chatSessions)..where((t) => t.id.equals(sessionId))).write(
          ChatSessionsCompanion(
            updatedAt: Value(msg.timestamp),
            lastMessageAt: Value(msg.timestamp),
            lastMessagePreview: Value(_truncateMessagePreview(msg.text)),
            messageCount: Value(nextSequence),
          ),
        );
      });

      if (userId != null && userId.isNotEmpty) {
        unawaited(
          _chatBackupService.enqueueMessageUpsert(
            userId: userId,
            sessionId: sessionId,
            messageId: msg.id,
          ),
        );
      }

      return const Result.success(null);
    } catch (e, st) {
      return Result.failure(
        AppException.unexpected(
          'Failed to persist message',
          error: e,
          stackTrace: st,
        ),
      );
    }
  }

  Future<Result<List<ChatMessageModel>>> loadMessages(
    String sessionId, {
    // Fetch the 200 newest messages by querying descending then reversing so
    // callers receive chronological order. Ascending + limit would return the
    // oldest 200 for long sessions, giving _buildHistory stale context.
    int limit = 200,
  }) async {
    try {
      final rows = await (_db.select(_db.chatMessages)
            ..where((t) => t.sessionId.equals(sessionId))
            ..orderBy([
              (t) => OrderingTerm.desc(t.sequence),
              (t) => OrderingTerm.desc(t.timestamp),
            ])
            ..limit(limit))
          .get();
      return Result.success(rows.reversed.map(_rowToModel).toList());
    } catch (e, st) {
      return Result.failure(
        AppException.unexpected(
          'Failed to load messages',
          error: e,
          stackTrace: st,
        ),
      );
    }
  }

  Future<Result<void>> setSessionTitle(
    String sessionId,
    String title, {
    String? userId,
  }) async {
    try {
      await (_db.update(_db.chatSessions)..where((t) => t.id.equals(sessionId))).write(
        ChatSessionsCompanion(
          title: Value(title),
          updatedAt: Value(DateTime.now()),
        ),
      );

      if (userId != null && userId.isNotEmpty) {
        unawaited(
          _chatBackupService.enqueueSessionUpsert(
            userId: userId,
            sessionId: sessionId,
          ),
        );
      }

      return const Result.success(null);
    } catch (e, st) {
      return Result.failure(
        AppException.unexpected(
          'Failed to update session title',
          error: e,
          stackTrace: st,
        ),
      );
    }
  }

  /// Updates the feedback (liked/disliked/null) on a single message.
  Future<Result<void>> updateFeedback(
    String messageId,
    MessageFeedback? feedback,
  ) async {
    try {
      await (_db.update(_db.chatMessages)..where((t) => t.id.equals(messageId))).write(
        ChatMessagesCompanion(
          feedback: Value(feedback?.name),
        ),
      );
      return const Result.success(null);
    } catch (e, st) {
      return Result.failure(
        AppException.unexpected(
          'Failed to update feedback',
          error: e,
          stackTrace: st,
        ),
      );
    }
  }

  /// Updates the status (and optional error reason) on a single message.
  Future<Result<void>> updateMessageStatus(
    String messageId,
    MessageStatus status, {
    String? errorReason,
  }) async {
    try {
      await (_db.update(_db.chatMessages)..where((t) => t.id.equals(messageId))).write(
        ChatMessagesCompanion(
          status: Value(status.name),
          errorReason: Value(errorReason),
        ),
      );
      return const Result.success(null);
    } catch (e, st) {
      return Result.failure(
        AppException.unexpected(
          'Failed to update message status',
          error: e,
          stackTrace: st,
        ),
      );
    }
  }

  /// Deletes a single message by ID.
  Future<Result<void>> deleteMessage(String messageId) async {
    try {
      await (_db.delete(_db.chatMessages)..where((t) => t.id.equals(messageId))).go();
      return const Result.success(null);
    } catch (e, st) {
      return Result.failure(
        AppException.unexpected(
          'Failed to delete message',
          error: e,
          stackTrace: st,
        ),
      );
    }
  }

  /// Deletes all messages in a session with sequence > [afterSequence].
  /// Used by the edit feature to remove everything after the edited message.
  Future<Result<void>> deleteMessagesAfter(
    String sessionId,
    int afterSequence,
  ) async {
    try {
      await (_db.delete(_db.chatMessages)
            ..where(
              (t) => t.sessionId.equals(sessionId) & t.sequence.isBiggerThanValue(afterSequence),
            ))
          .go();
      return const Result.success(null);
    } catch (e, st) {
      return Result.failure(
        AppException.unexpected(
          'Failed to delete messages',
          error: e,
          stackTrace: st,
        ),
      );
    }
  }

  /// Updates message content (for edit). Returns the updated row count.
  Future<Result<void>> updateMessageContent(
    String messageId,
    String newContent,
  ) async {
    try {
      await (_db.update(_db.chatMessages)..where((t) => t.id.equals(messageId))).write(
        ChatMessagesCompanion(
          content: Value(newContent),
        ),
      );
      return const Result.success(null);
    } catch (e, st) {
      return Result.failure(
        AppException.unexpected(
          'Failed to update message content',
          error: e,
          stackTrace: st,
        ),
      );
    }
  }

  /// Looks up a single message's sequence number by ID.
  Future<int?> getMessageSequence(String messageId) async {
    final row = await (_db.select(_db.chatMessages)
          ..where((t) => t.id.equals(messageId)))
        .getSingleOrNull();
    return row?.sequence;
  }

  static String _truncateMessagePreview(String text) {
    final normalized = text.replaceAll(RegExp(r'\s+'), ' ').trim();
    if (normalized.length <= 160) {
      return normalized;
    }
    return '${normalized.substring(0, 157)}...';
  }

  static ChatMessageModel _rowToModel(ChatMessage row) {
    return ChatMessageModel(
      id: row.id,
      text: row.content,
      isUser: row.isUser,
      timestamp: row.timestamp,
      channel: ChatMessageChannel.values.firstWhere(
        (c) => c.name == row.channel,
        orElse: () => ChatMessageChannel.text,
      ),
      status: row.status == null
          ? MessageStatus.sent
          : MessageStatus.values.firstWhere(
              (s) => s.name == row.status,
              orElse: () => MessageStatus.sent,
            ),
      feedback: row.feedback == null
          ? null
          : MessageFeedback.values.firstWhere(
              (f) => f.name == row.feedback,
              orElse: () => MessageFeedback.liked,
            ),
      errorReason: row.errorReason,
      sessionId: row.sessionId,
      engagementId: row.engagementId,
      engagementAgent: row.engagementAgent,
      reminderPayload: ReminderPayload.tryFromJsonString(row.reminderJson),
      clarificationPayload:
          ClarificationPayload.tryFromJsonString(row.clarificationJson),
    );
  }
}
