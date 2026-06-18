import 'dart:async';
import 'dart:convert';
import 'dart:math' as math;

import 'package:cloud_firestore/cloud_firestore.dart' hide Constant;
import 'package:drift/drift.dart';
import 'package:firebase_core/firebase_core.dart';
import 'package:shared_preferences/shared_preferences.dart';

import '../../core/logging/app_logger.dart';
import '../local/app_database.dart';

enum ChatSyncJobType { sessionUpsert, messageUpsert }

/// Backfill is bounded to the same session window restore covers, so a one-time
/// repair can never enqueue unbounded work.
const int _backfillSessionLimit = 25;

/// After this many failed sync attempts a job is treated as stuck and logged
/// loudly (once), so a permanently-failing backup is visible instead of looping
/// silently until the user reinstalls and loses data.
const int _stuckJobAttemptThreshold = 8;

class ChatBackupService {
  final AppDatabase _db;
  final FirebaseFirestore? _firestore;

  bool _isProcessing = false;
  Timer? _retryTimer;

  ChatBackupService({
    required AppDatabase db,
    FirebaseFirestore? firestore,
  })  : _db = db,
        _firestore = firestore ?? _resolveFirestore();

  static FirebaseFirestore? _resolveFirestore() {
    try {
      if (Firebase.apps.isEmpty) return null;
      return FirebaseFirestore.instance;
    } catch (_) {
      return null;
    }
  }

  Future<void> enqueueMessageUpsert({
    required String userId,
    required String sessionId,
    required String messageId,
  }) async {
    await _db.into(_db.chatSyncJobs).insert(
          ChatSyncJobsCompanion.insert(
            userId: userId,
            sessionId: sessionId,
            messageId: Value(messageId),
            jobType: ChatSyncJobType.messageUpsert.name,
          ),
        );
    unawaited(processPendingJobs(userId: userId));
  }

  Future<void> enqueueSessionUpsert({
    required String userId,
    required String sessionId,
  }) async {
    await _db.into(_db.chatSyncJobs).insert(
          ChatSyncJobsCompanion.insert(
            userId: userId,
            sessionId: sessionId,
            jobType: ChatSyncJobType.sessionUpsert.name,
          ),
        );
    unawaited(processPendingJobs(userId: userId));
  }

  Future<void> processPendingJobs({String? userId}) async {
    final firestore = _firestore;
    if (firestore == null || _isProcessing) return;

    _retryTimer?.cancel();
    _isProcessing = true;

    try {
      while (true) {
        final job = await _nextDueJob(userId: userId);
        if (job == null) {
          await _scheduleNextRetry(userId: userId);
          break;
        }

        final processed = await _processJob(job, firestore);
        if (processed) {
          await (_db.delete(_db.chatSyncJobs)..where((t) => t.id.equals(job.id))).go();
          continue;
        }

        await _scheduleNextRetry(userId: userId);
        break;
      }
    } finally {
      _isProcessing = false;
    }
  }

  /// Pull messages for a single session from Firestore when Drift has none locally.
  ///
  /// Called from ChatViewModel._loadSession before loadMessages so that a user
  /// opening an old thread on a fresh install (or after a cache clear) gets their
  /// full history back instead of an empty chat that forgets prior context.
  ///
  /// Uses localCount == 0 as the trigger, not session.messageCount, because
  /// deleteMessage and deleteMessagesAfter never decrement messageCount (counter
  /// drift makes that field unreliable as a completeness signal).
  Future<bool> restoreSessionMessagesIfEmpty(
    String userId,
    String sessionId,
  ) async {
    final firestore = _firestore;
    if (userId.isEmpty || firestore == null) return false;

    final localCount = await _localMessageCountForSession(sessionId);
    if (localCount > 0) return false;

    try {
      final snap = await firestore
          .collection('users')
          .doc(userId)
          .collection('chat_sessions')
          .doc(sessionId)
          .collection('messages')
          .orderBy('sequence')
          .get();

      if (snap.docs.isEmpty) return false;

      await _db.transaction(() async {
        for (final doc in snap.docs) {
          final d = doc.data();
          await _db.into(_db.chatMessages).insertOnConflictUpdate(
            ChatMessagesCompanion.insert(
              id: doc.id,
              sessionId: sessionId,
              content: (d['text'] as String?) ?? '',
              isUser: (d['role'] as String?) == 'user',
              channel: (d['channel'] as String?) ?? 'text',
              timestamp: _toDateTime(d['created_at']) ?? DateTime.now(),
              sequence: Value((d['sequence'] as num?)?.toInt() ?? 0),
              // Full metadata round-trips back into Drift. Older backup docs that
              // predate this enrichment simply lack these keys and read as null.
              status: Value(d['status'] as String?),
              feedback: Value(d['feedback'] as String?),
              errorReason: Value(d['error_reason'] as String?),
              engagementId: Value(d['engagement_id'] as String?),
              engagementAgent: Value(d['engagement_agent'] as String?),
              reminderJson: Value(_encodeJsonField(d['reminder'])),
              clarificationJson: Value(_encodeJsonField(d['clarification'])),
              attachmentJson: Value(_encodeJsonField(d['attachments'])),
            ),
          );
        }
      });

      AppLogger.info(
        'Restored session messages from Firestore backup',
        tag: 'ChatBackupService',
        metadata: {
          'userId': userId,
          'sessionId': sessionId,
          'messageCount': snap.docs.length,
        },
      );
      return true;
    } catch (e, st) {
      AppLogger.error(
        'Failed to restore session messages from backup',
        error: e,
        stackTrace: st,
        tag: 'ChatBackupService',
        metadata: {'userId': userId, 'sessionId': sessionId},
      );
      return false;
    }
  }

  /// Count messages stored locally in Drift for a specific session.
  Future<int> _localMessageCountForSession(String sessionId) async {
    final countExpr = _db.chatMessages.id.count();
    final query = _db.selectOnly(_db.chatMessages)
      ..addColumns([countExpr])
      ..where(_db.chatMessages.sessionId.equals(sessionId));
    final row = await query.getSingle();
    return row.read(countExpr) ?? 0;
  }

  Future<bool> restoreFromBackupIfLocalEmpty(String userId) async {
    final firestore = _firestore;
    if (userId.isEmpty || firestore == null) {
      return false;
    }

    if (await _localSessionCountForUser(userId) > 0) {
      return false;
    }

    try {
      final sessionsSnapshot = await firestore
          .collection('users')
          .doc(userId)
          .collection('chat_sessions')
          .orderBy('updated_at', descending: true)
          .limit(25)
          .get();

      if (sessionsSnapshot.docs.isEmpty) {
        return false;
      }

      final restoredSessions = <ChatSessionsCompanion>[];

      for (final sessionDoc in sessionsSnapshot.docs) {
        final data = sessionDoc.data();
        final startedAt = _toDateTime(data['started_at']) ?? DateTime.now();
        final updatedAt = _toDateTime(data['updated_at']) ?? startedAt;
        final lastMessageAt = _toDateTime(data['last_message_at']);
        restoredSessions.add(
          ChatSessionsCompanion.insert(
            id: sessionDoc.id,
            userId: Value(userId),
            startedAt: startedAt,
            updatedAt: Value(updatedAt),
            title: Value(data['title'] as String?),
            lastMessageAt: Value(lastMessageAt),
            lastMessagePreview: Value(data['last_message_preview'] as String?),
            messageCount: Value((data['message_count'] as num?)?.toInt() ?? 0),
            agentId: Value(data['agent_id'] as String?),
          ),
        );
      }

      if (await _localSessionCountForUser(userId) > 0) {
        return false;
      }

      await _db.transaction(() async {
        for (final session in restoredSessions) {
          await _db.into(_db.chatSessions).insertOnConflictUpdate(session);
        }
      });

      AppLogger.info(
        'Restored chat session metadata from Firestore backup',
        tag: 'ChatBackupService',
        metadata: {
          'userId': userId,
          'sessionCount': restoredSessions.length,
        },
      );
      return true;
    } catch (e, st) {
      AppLogger.error(
        'Failed to restore chat backup',
        error: e,
        stackTrace: st,
        tag: 'ChatBackupService',
        metadata: {'userId': userId},
      );
      return false;
    }
  }

  /// One-time repair: enqueue local chat history that predates reliable sync so
  /// it reaches Firestore and survives a reinstall. Gated by a per-user flag so
  /// it runs once per install, and bounded to the [_backfillSessionLimit] most
  /// recent sessions (the same window restore covers). Idempotent — Firestore
  /// writes merge, so re-running would be safe even if the flag were cleared.
  Future<void> backfillUnsynced(String userId) async {
    final firestore = _firestore;
    if (userId.isEmpty || firestore == null) return;

    final prefs = await SharedPreferences.getInstance();
    // v2 bump: re-runs the one-time backfill so messages synced under the old
    // (metadata-lossy) writer get re-pushed from Drift, which holds every field.
    // Bounded to _backfillSessionLimit recent sessions and idempotent (merge writes)
    final flagKey = 'chat_backfill_v2_$userId';
    if (prefs.getBool(flagKey) ?? false) return;

    try {
      final sessions = await (_db.select(_db.chatSessions)
            ..where((t) => t.userId.equals(userId))
            ..orderBy([(t) => OrderingTerm.desc(t.updatedAt)])
            ..limit(_backfillSessionLimit))
          .get();

      var enqueued = 0;
      await _db.transaction(() async {
        for (final session in sessions) {
          await _db.into(_db.chatSyncJobs).insert(
                ChatSyncJobsCompanion.insert(
                  userId: userId,
                  sessionId: session.id,
                  jobType: ChatSyncJobType.sessionUpsert.name,
                ),
              );
          enqueued++;

          final messages = await (_db.select(_db.chatMessages)
                ..where((t) => t.sessionId.equals(session.id)))
              .get();
          for (final message in messages) {
            await _db.into(_db.chatSyncJobs).insert(
                  ChatSyncJobsCompanion.insert(
                    userId: userId,
                    sessionId: session.id,
                    messageId: Value(message.id),
                    jobType: ChatSyncJobType.messageUpsert.name,
                  ),
                );
            enqueued++;
          }
        }
      });

      await prefs.setBool(flagKey, true);

      if (enqueued > 0) {
        AppLogger.info(
          'Backfill enqueued unsynced chat history',
          tag: 'ChatBackupService',
          metadata: {
            'userId': userId,
            'sessions': sessions.length,
            'jobs': enqueued,
          },
        );
        unawaited(processPendingJobs(userId: userId));
      }
    } catch (e, st) {
      AppLogger.error(
        'Backfill failed',
        error: e,
        stackTrace: st,
        tag: 'ChatBackupService',
        metadata: {'userId': userId},
      );
    }
  }

  Future<bool> _processJob(
    ChatSyncJob job,
    FirebaseFirestore firestore,
  ) async {
    try {
      final session = await _sessionById(job.sessionId);
      if (session == null) {
        return true;
      }

      final sessionRef = firestore
          .collection('users')
          .doc(job.userId)
          .collection('chat_sessions')
          .doc(job.sessionId);

      final batch = firestore.batch();
      batch.set(sessionRef, _sessionDoc(session), SetOptions(merge: true));

      if (job.jobType == ChatSyncJobType.messageUpsert.name) {
        final messageId = job.messageId;
        if (messageId == null) {
          return true;
        }

        final message = await _messageById(messageId);
        if (message == null) {
          return true;
        }

        final messageRef = sessionRef.collection('messages').doc(message.id);
        batch.set(messageRef, _messageDoc(message), SetOptions(merge: true));
      }

      await batch.commit();
      return true;
    } catch (e, st) {
      await _markJobFailed(job, e);
      AppLogger.error(
        'Chat backup sync failed',
        error: e,
        stackTrace: st,
        tag: 'ChatBackupService',
        metadata: {
          'jobId': job.id,
          'jobType': job.jobType,
          'sessionId': job.sessionId,
          'messageId': job.messageId,
          'userId': job.userId,
        },
      );
      return false;
    }
  }

  Map<String, dynamic> _sessionDoc(ChatSession session) {
    return {
      'title': session.title,
      'started_at': Timestamp.fromDate(session.startedAt.toUtc()),
      'updated_at': Timestamp.fromDate(session.updatedAt.toUtc()),
      if (session.lastMessageAt != null)
        'last_message_at': Timestamp.fromDate(session.lastMessageAt!.toUtc()),
      if (session.lastMessagePreview != null)
        'last_message_preview': session.lastMessagePreview,
      'message_count': session.messageCount,
      if (session.agentId != null) 'agent_id': session.agentId,
    };
  }

  Map<String, dynamic> _messageDoc(ChatMessage message) {
    // Structured payloads are stored in Drift as JSON strings. We decode them to
    // nested maps so the Firestore backup is human-readable in the console (the
    // whole reason this layout exists) and so restore can re-encode them losslessly.
    final reminder = _decodeJsonObject(message.reminderJson);
    final clarification = _decodeJsonObject(message.clarificationJson);
    // attachment_json is metadata-only by design (fileName/mimeType/type/thumbnail);
    // raw file bytes are never persisted to Drift, so nothing sensitive is uploaded.
    final attachments = _decodeJsonArray(message.attachmentJson);

    return {
      'session_id': message.sessionId,
      'role': message.isUser ? 'user' : 'assistant',
      'text': message.content,
      'channel': message.channel,
      'created_at': Timestamp.fromDate(message.timestamp.toUtc()),
      'sequence': message.sequence,
      // Default mirrors ChatMessageModel.fromMap, which treats a null status as 'sent'.
      'status': message.status ?? 'sent',
      if (message.feedback != null) 'feedback': message.feedback,
      if (message.errorReason != null) 'error_reason': message.errorReason,
      if (message.engagementId != null) 'engagement_id': message.engagementId,
      if (message.engagementAgent != null)
        'engagement_agent': message.engagementAgent,
      'reminder': ?reminder,
      'clarification': ?clarification,
      'attachments': ?attachments,
    };
  }

  /// Decodes a Drift-stored JSON object string into a map, or null if absent/invalid.
  Map<String, dynamic>? _decodeJsonObject(String? raw) {
    if (raw == null || raw.isEmpty) return null;
    try {
      final decoded = jsonDecode(raw);
      return decoded is Map<String, dynamic> ? decoded : null;
    } catch (_) {
      return null;
    }
  }

  /// Decodes a Drift-stored JSON array string into a list, or null if empty/invalid.
  List<dynamic>? _decodeJsonArray(String? raw) {
    if (raw == null || raw.isEmpty) return null;
    try {
      final decoded = jsonDecode(raw);
      return (decoded is List && decoded.isNotEmpty) ? decoded : null;
    } catch (_) {
      return null;
    }
  }

  /// Re-encodes a backed-up nested map/list back to the JSON string Drift expects.
  /// Empty collections become null so the local row matches "no payload".
  String? _encodeJsonField(Object? value) {
    if (value is Map && value.isNotEmpty) return jsonEncode(value);
    if (value is List && value.isNotEmpty) return jsonEncode(value);
    return null;
  }

  Future<void> _markJobFailed(ChatSyncJob job, Object error) async {
    final nextAttemptCount = job.attemptCount + 1;
    final delay = _retryDelay(nextAttemptCount);
    final errorText = error.toString();
    final truncatedError = errorText.length > 500
        ? errorText.substring(0, 500)
        : errorText;

    await (_db.update(_db.chatSyncJobs)..where((t) => t.id.equals(job.id))).write(
      ChatSyncJobsCompanion(
        attemptCount: Value(nextAttemptCount),
        nextAttemptAt: Value(DateTime.now().add(delay)),
        lastError: Value(truncatedError),
      ),
    );

    // Fail loud once when a job crosses the stuck threshold — a permanently
    // failing sync would otherwise loop silently and the user would lose this
    // message on reinstall with no signal anywhere.
    if (nextAttemptCount == _stuckJobAttemptThreshold) {
      AppLogger.error(
        'Chat backup job stuck, repeated sync failures',
        tag: 'ChatBackupService',
        metadata: {
          'jobId': job.id,
          'userId': job.userId,
          'sessionId': job.sessionId,
          'attempts': nextAttemptCount,
          'lastError': truncatedError,
        },
      );
    }
  }

  Duration _retryDelay(int attemptCount) {
    final seconds = math.min(60, 1 << math.min(attemptCount, 6));
    return Duration(seconds: seconds);
  }

  Future<ChatSyncJob?> _nextDueJob({String? userId}) {
    final now = DateTime.now();
    final query = _db.select(_db.chatSyncJobs)
      ..where(
        (t) => userId == null || userId.isEmpty
            ? t.nextAttemptAt.isSmallerOrEqualValue(now)
            : t.nextAttemptAt.isSmallerOrEqualValue(now) & t.userId.equals(userId),
      )
      ..orderBy([
        (t) => OrderingTerm.asc(t.nextAttemptAt),
        (t) => OrderingTerm.asc(t.id),
      ])
      ..limit(1);
    return query.getSingleOrNull();
  }

  Future<ChatSyncJob?> _nextScheduledJob({String? userId}) {
    final query = _db.select(_db.chatSyncJobs)
      ..where(
        (t) => userId == null || userId.isEmpty ? const Constant(true) : t.userId.equals(userId),
      )
      ..orderBy([
        (t) => OrderingTerm.asc(t.nextAttemptAt),
        (t) => OrderingTerm.asc(t.id),
      ])
      ..limit(1);
    return query.getSingleOrNull();
  }

  Future<void> _scheduleNextRetry({String? userId}) async {
    final nextJob = await _nextScheduledJob(userId: userId);
    if (nextJob == null) return;

    final delay = nextJob.nextAttemptAt.difference(DateTime.now());
    _retryTimer?.cancel();
    _retryTimer = Timer(
      delay.isNegative ? Duration.zero : delay,
      () => unawaited(processPendingJobs(userId: userId)),
    );
  }

  Future<ChatSession?> _sessionById(String sessionId) {
    return (_db.select(_db.chatSessions)..where((t) => t.id.equals(sessionId))).getSingleOrNull();
  }

  Future<ChatMessage?> _messageById(String messageId) {
    return (_db.select(_db.chatMessages)..where((t) => t.id.equals(messageId))).getSingleOrNull();
  }

  Future<int> _localSessionCountForUser(String userId) async {
    final countExpression = _db.chatSessions.id.count();
    final query = _db.selectOnly(_db.chatSessions)
      ..addColumns([countExpression])
      ..where(_db.chatSessions.userId.equals(userId));
    final row = await query.getSingle();
    return row.read(countExpression) ?? 0;
  }

  DateTime? _toDateTime(Object? raw) {
    if (raw is Timestamp) return raw.toDate();
    if (raw is DateTime) return raw;
    if (raw is String) return DateTime.tryParse(raw);
    return null;
  }
}
