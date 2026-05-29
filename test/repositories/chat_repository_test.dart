import 'package:drift/native.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:mockito/annotations.dart';
import 'package:mockito/mockito.dart';

import 'package:aura/data/local/app_database.dart';
import 'package:aura/data/models/chat_message_model.dart';
import 'package:aura/data/repositories/chat_repository.dart';
import 'package:aura/data/services/chat_backup_service.dart';

import 'chat_repository_test.mocks.dart';

@GenerateNiceMocks([MockSpec<ChatBackupService>()])
ChatMessageModel _msg({
  required String id,
  required String text,
  required bool isUser,
  required String? sessionId,
  DateTime? timestamp,
}) {
  return ChatMessageModel(
    id: id,
    text: text,
    isUser: isUser,
    timestamp: timestamp ?? DateTime(2026, 1, 1, 12),
    channel: ChatMessageChannel.text,
    sessionId: sessionId,
  );
}

void main() {
  late AppDatabase db;
  late MockChatBackupService backup;
  late ChatRepository repo;

  setUp(() {
    db = AppDatabase.forTesting(NativeDatabase.memory());
    backup = MockChatBackupService();
    when(backup.enqueueMessageUpsert(
      userId: anyNamed('userId'),
      sessionId: anyNamed('sessionId'),
      messageId: anyNamed('messageId'),
    )).thenAnswer((_) async {});
    when(backup.enqueueSessionUpsert(
      userId: anyNamed('userId'),
      sessionId: anyNamed('sessionId'),
    )).thenAnswer((_) async {});
    repo = ChatRepository(db: db, chatBackupService: backup);
  });

  tearDown(() async => db.close());

  test('saveMessage persists and increments sequence', () async {
    final sessionId = await repo.createSession(userId: 'u1');

    final r1 = await repo.saveMessage(
      _msg(id: 'm1', text: 'first', isUser: true, sessionId: sessionId),
    );
    final r2 = await repo.saveMessage(
      _msg(id: 'm2', text: 'second', isUser: false, sessionId: sessionId),
    );

    expect(r1.isSuccess, isTrue);
    expect(r2.isSuccess, isTrue);
    expect(await repo.getMessageSequence('m1'), 1);
    expect(await repo.getMessageSequence('m2'), 2);
  });

  test('saveMessage with null sessionId fails', () async {
    final result = await repo.saveMessage(
      _msg(id: 'm1', text: 'x', isUser: true, sessionId: null),
    );
    expect(result.isFailure, isTrue);
  });

  test('saveMessage to a missing session fails', () async {
    final result = await repo.saveMessage(
      _msg(id: 'm1', text: 'x', isUser: true, sessionId: 'does-not-exist'),
    );
    expect(result.isFailure, isTrue);
  });

  test('loadMessages returns chronological order', () async {
    final sessionId = await repo.createSession(userId: 'u1');
    await repo.saveMessage(
      _msg(id: 'm1', text: 'one', isUser: true, sessionId: sessionId),
    );
    await repo.saveMessage(
      _msg(id: 'm2', text: 'two', isUser: false, sessionId: sessionId),
    );
    await repo.saveMessage(
      _msg(id: 'm3', text: 'three', isUser: true, sessionId: sessionId),
    );

    final result = await repo.loadMessages(sessionId);

    expect(result.isSuccess, isTrue);
    final texts = result.dataOrNull!.map((m) => m.text).toList();
    expect(texts, ['one', 'two', 'three']);
  });

  test('deleteMessagesAfter removes only later messages', () async {
    final sessionId = await repo.createSession(userId: 'u1');
    await repo.saveMessage(
      _msg(id: 'm1', text: 'one', isUser: true, sessionId: sessionId),
    );
    await repo.saveMessage(
      _msg(id: 'm2', text: 'two', isUser: false, sessionId: sessionId),
    );
    await repo.saveMessage(
      _msg(id: 'm3', text: 'three', isUser: true, sessionId: sessionId),
    );

    await repo.deleteMessagesAfter(sessionId, 1);

    final result = await repo.loadMessages(sessionId);
    final ids = result.dataOrNull!.map((m) => m.id).toList();
    expect(ids, ['m1']);
  });

  test('getSessionsForAgent filters by agent and excludes empty sessions',
      () async {
    final mainSession = await repo.createSession(userId: 'u1');
    final agentSession = await repo.createSession(userId: 'u1', agentId: 'sports');
    // Empty session (no messages) should be excluded.
    await repo.createSession(userId: 'u1');

    await repo.saveMessage(
      _msg(id: 'a', text: 'main', isUser: true, sessionId: mainSession),
    );
    await repo.saveMessage(
      _msg(id: 'b', text: 'agent', isUser: true, sessionId: agentSession),
    );

    final mainResult = await repo.getSessionsForAgent(userId: 'u1', agentId: null);
    final agentResult =
        await repo.getSessionsForAgent(userId: 'u1', agentId: 'sports');

    expect(mainResult.dataOrNull!.map((s) => s.id), [mainSession]);
    expect(agentResult.dataOrNull!.map((s) => s.id), [agentSession]);
  });

  test('updateFeedback / updateMessageStatus / updateMessageContent round-trip',
      () async {
    final sessionId = await repo.createSession(userId: 'u1');
    await repo.saveMessage(
      _msg(id: 'm1', text: 'orig', isUser: false, sessionId: sessionId),
    );

    await repo.updateFeedback('m1', MessageFeedback.liked);
    await repo.updateMessageStatus('m1', MessageStatus.error,
        errorReason: 'boom');
    await repo.updateMessageContent('m1', 'edited');

    final msg = (await repo.loadMessages(sessionId)).dataOrNull!.single;
    expect(msg.feedback, MessageFeedback.liked);
    expect(msg.status, MessageStatus.error);
    expect(msg.errorReason, 'boom');
    expect(msg.text, 'edited');
  });
}
