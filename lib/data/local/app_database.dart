import 'package:drift/drift.dart';
import 'package:drift_flutter/drift_flutter.dart';

part 'app_database.g.dart';

class ChatSessions extends Table {
  TextColumn get id => text()();
  // v8: scopes sessions to the authenticated user
  TextColumn get userId => text().withDefault(const Constant(''))();
  DateTimeColumn get startedAt => dateTime()();
  DateTimeColumn get updatedAt => dateTime().withDefault(currentDateAndTime)();
  TextColumn get title => text().nullable()();
  DateTimeColumn get lastMessageAt => dateTime().nullable()();
  TextColumn get lastMessagePreview => text().nullable()();
  IntColumn get messageCount => integer().withDefault(const Constant(0))();
  TextColumn get agentId => text().nullable()();

  @override
  Set<Column> get primaryKey => {id};
}

class ChatMessages extends Table {
  TextColumn get id => text()();
  TextColumn get sessionId =>
      text().references(ChatSessions, #id, onDelete: KeyAction.cascade)();
  TextColumn get content => text()();
  BoolColumn get isUser => boolean()();
  TextColumn get channel => text()();
  DateTimeColumn get timestamp => dateTime()();
  IntColumn get sequence => integer().withDefault(const Constant(0))();
  TextColumn get feedback => text().nullable()();
  TextColumn get status => text().nullable()();
  TextColumn get errorReason => text().nullable()();
  // v4: engagement context — set when message is pre-inserted from an FCM tap
  TextColumn get engagementId => text().nullable()();
  TextColumn get engagementAgent => text().nullable()();
  // v5: reminder payload JSON — set when assistant called set_reminder this turn
  TextColumn get reminderJson => text().nullable()();
  // v6: clarification payload JSON — set when assistant called ask_clarification
  TextColumn get clarificationJson => text().nullable()();
  // v9: attachment metadata + thumbnails — JSON array, in-memory bytes not stored
  TextColumn get attachmentJson => text().nullable()();
  // v10: how the user entered this message — 'typed' | 'pasted'. Null for
  // assistant messages and legacy rows written before capture existed.
  TextColumn get inputMethod => text().nullable()();

  @override
  Set<Column> get primaryKey => {id};
}

class ChatSyncJobs extends Table {
  IntColumn get id => integer().autoIncrement()();
  TextColumn get userId => text()();
  TextColumn get sessionId => text()();
  TextColumn get messageId => text().nullable()();
  TextColumn get jobType => text()();
  DateTimeColumn get createdAt => dateTime().withDefault(currentDateAndTime)();
  DateTimeColumn get nextAttemptAt =>
      dateTime().withDefault(currentDateAndTime)();
  IntColumn get attemptCount => integer().withDefault(const Constant(0))();
  TextColumn get lastError => text().nullable()();
}

class GetBetterCatalogCaches extends Table {
  TextColumn get cacheKey => text()();
  TextColumn get catalogVersion => text()();
  TextColumn get feedJson => text()();
  DateTimeColumn get checkedAt => dateTime()();
  DateTimeColumn get updatedAt => dateTime()();

  @override
  Set<Column> get primaryKey => {cacheKey};
}

class GetBetterStoryProgress extends Table {
  TextColumn get userId => text()();
  TextColumn get storyId => text()();
  IntColumn get storyVersion => integer().withDefault(const Constant(1))();
  BoolColumn get opened => boolean().withDefault(const Constant(false))();
  BoolColumn get saved => boolean().withDefault(const Constant(false))();
  BoolColumn get completed => boolean().withDefault(const Constant(false))();
  DateTimeColumn get lastOpenedAt => dateTime().nullable()();
  DateTimeColumn get updatedAt => dateTime().withDefault(currentDateAndTime)();

  @override
  Set<Column> get primaryKey => {userId, storyId};
}

class GetBetterEventOutbox extends Table {
  TextColumn get eventId => text()();
  TextColumn get userId => text()();
  TextColumn get eventType => text()();
  TextColumn get storyId => text()();
  IntColumn get storyVersion => integer()();
  DateTimeColumn get occurredAt => dateTime()();

  @override
  Set<Column> get primaryKey => {eventId};
}

@DriftDatabase(
  tables: [
    ChatSessions,
    ChatMessages,
    ChatSyncJobs,
    GetBetterCatalogCaches,
    GetBetterStoryProgress,
    GetBetterEventOutbox,
  ],
)
class AppDatabase extends _$AppDatabase {
  AppDatabase() : super(_createDatabaseConnection());

  /// Test-only: inject an in-memory or custom executor so unit tests can run
  /// against a real SQLite database without touching the on-device file.
  AppDatabase.forTesting(super.executor);

  @override
  int get schemaVersion => 11;

  @override
  MigrationStrategy get migration => MigrationStrategy(
    onCreate: (m) async {
      await m.createAll();
    },
    onUpgrade: (m, from, to) async {
      if (from < 2) {
        // updatedAt has a non-constant default (currentDateAndTime) which
        // SQLite rejects in ALTER TABLE ADD COLUMN. Use a literal 0; the
        // UPDATE statement below immediately sets the correct value.
        await customStatement(
          'ALTER TABLE "chat_sessions" ADD COLUMN "updated_at" INTEGER NOT NULL DEFAULT 0',
        );
        await m.addColumn(chatSessions, chatSessions.lastMessageAt);
        await m.addColumn(chatSessions, chatSessions.lastMessagePreview);
        await m.addColumn(chatSessions, chatSessions.messageCount);
        await m.addColumn(chatMessages, chatMessages.sequence);
        await m.createTable(chatSyncJobs);

        await customStatement('''
              UPDATE chat_messages
              SET sequence = (
                SELECT COUNT(*)
                FROM chat_messages AS earlier
                WHERE earlier.session_id = chat_messages.session_id
                  AND (
                    earlier.timestamp < chat_messages.timestamp
                    OR (
                      earlier.timestamp = chat_messages.timestamp
                      AND earlier.id <= chat_messages.id
                    )
                  )
              )
            ''');

        await customStatement('''
              UPDATE chat_sessions
              SET
                message_count = COALESCE((
                  SELECT MAX(sequence)
                  FROM chat_messages
                  WHERE session_id = chat_sessions.id
                ), 0),
                last_message_at = (
                  SELECT timestamp
                  FROM chat_messages
                  WHERE session_id = chat_sessions.id
                  ORDER BY sequence DESC
                  LIMIT 1
                ),
                last_message_preview = (
                  SELECT substr(content, 1, 160)
                  FROM chat_messages
                  WHERE session_id = chat_sessions.id
                  ORDER BY sequence DESC
                  LIMIT 1
                ),
                updated_at = COALESCE((
                  SELECT timestamp
                  FROM chat_messages
                  WHERE session_id = chat_sessions.id
                  ORDER BY sequence DESC
                  LIMIT 1
                ), started_at)
            ''');
      }
      if (from < 3) {
        await customStatement(
          'ALTER TABLE "chat_messages" ADD COLUMN "feedback" TEXT',
        );
        await customStatement(
          'ALTER TABLE "chat_messages" ADD COLUMN "status" TEXT',
        );
        await customStatement(
          'ALTER TABLE "chat_messages" ADD COLUMN "error_reason" TEXT',
        );
      }
      if (from < 4) {
        await customStatement(
          'ALTER TABLE "chat_messages" ADD COLUMN "engagement_id" TEXT',
        );
        await customStatement(
          'ALTER TABLE "chat_messages" ADD COLUMN "engagement_agent" TEXT',
        );
      }
      if (from < 5) {
        await customStatement(
          'ALTER TABLE "chat_messages" ADD COLUMN "reminder_json" TEXT',
        );
      }
      if (from < 6) {
        await customStatement(
          'ALTER TABLE "chat_messages" ADD COLUMN "clarification_json" TEXT',
        );
      }
      if (from < 7) {
        await customStatement(
          'ALTER TABLE "chat_sessions" ADD COLUMN "agent_id" TEXT',
        );
      }
      if (from < 8) {
        await customStatement(
          'ALTER TABLE "chat_sessions" ADD COLUMN "user_id" TEXT NOT NULL DEFAULT \'\'',
        );
      }
      if (from < 9) {
        await customStatement(
          'ALTER TABLE "chat_messages" ADD COLUMN "attachment_json" TEXT',
        );
      }
      if (from < 10) {
        await customStatement(
          'ALTER TABLE "chat_messages" ADD COLUMN "input_method" TEXT',
        );
      }
      if (from < 11) {
        await m.createTable(getBetterCatalogCaches);
        await m.createTable(getBetterStoryProgress);
        await m.createTable(getBetterEventOutbox);
      }
    },
    beforeOpen: (details) async {
      await customStatement('PRAGMA foreign_keys = ON');
    },
  );

  static QueryExecutor _createDatabaseConnection() {
    return driftDatabase(name: 'juno_chat');
  }
}
