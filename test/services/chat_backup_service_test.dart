import 'package:drift/native.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:aura/data/local/app_database.dart';
import 'package:aura/data/services/chat_backup_service.dart';

/// Queue-shape tests for the write-reduction change: a message no longer rewrites
/// its parent session, and session-metadata jobs coalesce to one pending per session.
/// Firestore is absent in a unit test, so the drain no-ops and enqueued jobs remain
/// in the Drift queue for inspection.
void main() {
  late AppDatabase db;
  late ChatBackupService service;

  setUp(() {
    db = AppDatabase.forTesting(NativeDatabase.memory());
    service = ChatBackupService(db: db);
  });

  tearDown(() async => db.close());

  Future<List<ChatSyncJob>> allJobs() => db.select(db.chatSyncJobs).get();

  List<ChatSyncJob> ofType(List<ChatSyncJob> jobs, ChatSyncJobType type) =>
      jobs.where((j) => j.jobType == type.name).toList();

  test('session upserts coalesce to one pending job per session', () async {
    for (var i = 0; i < 5; i++) {
      await service.enqueueSessionUpsert(userId: 'u1', sessionId: 's1');
    }
    await service.enqueueSessionUpsert(userId: 'u1', sessionId: 's2');

    final sessionJobs = ofType(await allJobs(), ChatSyncJobType.sessionUpsert);
    expect(sessionJobs.length, 2);
    expect(sessionJobs.map((j) => j.sessionId).toSet(), {'s1', 's2'});
  });

  test('message upserts are never coalesced (each message is distinct)', () async {
    await service.enqueueMessageUpsert(userId: 'u1', sessionId: 's1', messageId: 'm1');
    await service.enqueueMessageUpsert(userId: 'u1', sessionId: 's1', messageId: 'm2');
    await service.enqueueMessageUpsert(userId: 'u1', sessionId: 's1', messageId: 'm3');

    final messageJobs = ofType(await allJobs(), ChatSyncJobType.messageUpsert);
    expect(messageJobs.length, 3);
    expect(messageJobs.map((j) => j.messageId).toSet(), {'m1', 'm2', 'm3'});
  });

  test('a 12-message burst leaves 12 child jobs + 1 coalesced session job', () async {
    // Mirrors the voice-transcript scenario: each saved message enqueues its own
    // child job plus a (coalescing) session job. Twelve of those collapse to one.
    for (var i = 0; i < 12; i++) {
      await service.enqueueMessageUpsert(userId: 'u1', sessionId: 's1', messageId: 'm$i');
      await service.enqueueSessionUpsert(userId: 'u1', sessionId: 's1');
    }

    final jobs = await allJobs();
    expect(ofType(jobs, ChatSyncJobType.messageUpsert).length, 12);
    expect(ofType(jobs, ChatSyncJobType.sessionUpsert).length, 1);
  });
}
