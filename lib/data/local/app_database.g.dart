// GENERATED CODE - DO NOT MODIFY BY HAND

part of 'app_database.dart';

// ignore_for_file: type=lint
class $ChatSessionsTable extends ChatSessions
    with TableInfo<$ChatSessionsTable, ChatSession> {
  @override
  final GeneratedDatabase attachedDatabase;
  final String? _alias;
  $ChatSessionsTable(this.attachedDatabase, [this._alias]);
  static const VerificationMeta _idMeta = const VerificationMeta('id');
  @override
  late final GeneratedColumn<String> id = GeneratedColumn<String>(
    'id',
    aliasedName,
    false,
    type: DriftSqlType.string,
    requiredDuringInsert: true,
  );
  static const VerificationMeta _userIdMeta = const VerificationMeta('userId');
  @override
  late final GeneratedColumn<String> userId = GeneratedColumn<String>(
    'user_id',
    aliasedName,
    false,
    type: DriftSqlType.string,
    requiredDuringInsert: false,
    defaultValue: const Constant(''),
  );
  static const VerificationMeta _startedAtMeta = const VerificationMeta(
    'startedAt',
  );
  @override
  late final GeneratedColumn<DateTime> startedAt = GeneratedColumn<DateTime>(
    'started_at',
    aliasedName,
    false,
    type: DriftSqlType.dateTime,
    requiredDuringInsert: true,
  );
  static const VerificationMeta _updatedAtMeta = const VerificationMeta(
    'updatedAt',
  );
  @override
  late final GeneratedColumn<DateTime> updatedAt = GeneratedColumn<DateTime>(
    'updated_at',
    aliasedName,
    false,
    type: DriftSqlType.dateTime,
    requiredDuringInsert: false,
    defaultValue: currentDateAndTime,
  );
  static const VerificationMeta _titleMeta = const VerificationMeta('title');
  @override
  late final GeneratedColumn<String> title = GeneratedColumn<String>(
    'title',
    aliasedName,
    true,
    type: DriftSqlType.string,
    requiredDuringInsert: false,
  );
  static const VerificationMeta _lastMessageAtMeta = const VerificationMeta(
    'lastMessageAt',
  );
  @override
  late final GeneratedColumn<DateTime> lastMessageAt =
      GeneratedColumn<DateTime>(
        'last_message_at',
        aliasedName,
        true,
        type: DriftSqlType.dateTime,
        requiredDuringInsert: false,
      );
  static const VerificationMeta _lastMessagePreviewMeta =
      const VerificationMeta('lastMessagePreview');
  @override
  late final GeneratedColumn<String> lastMessagePreview =
      GeneratedColumn<String>(
        'last_message_preview',
        aliasedName,
        true,
        type: DriftSqlType.string,
        requiredDuringInsert: false,
      );
  static const VerificationMeta _messageCountMeta = const VerificationMeta(
    'messageCount',
  );
  @override
  late final GeneratedColumn<int> messageCount = GeneratedColumn<int>(
    'message_count',
    aliasedName,
    false,
    type: DriftSqlType.int,
    requiredDuringInsert: false,
    defaultValue: const Constant(0),
  );
  static const VerificationMeta _agentIdMeta = const VerificationMeta(
    'agentId',
  );
  @override
  late final GeneratedColumn<String> agentId = GeneratedColumn<String>(
    'agent_id',
    aliasedName,
    true,
    type: DriftSqlType.string,
    requiredDuringInsert: false,
  );
  @override
  List<GeneratedColumn> get $columns => [
    id,
    userId,
    startedAt,
    updatedAt,
    title,
    lastMessageAt,
    lastMessagePreview,
    messageCount,
    agentId,
  ];
  @override
  String get aliasedName => _alias ?? actualTableName;
  @override
  String get actualTableName => $name;
  static const String $name = 'chat_sessions';
  @override
  VerificationContext validateIntegrity(
    Insertable<ChatSession> instance, {
    bool isInserting = false,
  }) {
    final context = VerificationContext();
    final data = instance.toColumns(true);
    if (data.containsKey('id')) {
      context.handle(_idMeta, id.isAcceptableOrUnknown(data['id']!, _idMeta));
    } else if (isInserting) {
      context.missing(_idMeta);
    }
    if (data.containsKey('user_id')) {
      context.handle(
        _userIdMeta,
        userId.isAcceptableOrUnknown(data['user_id']!, _userIdMeta),
      );
    }
    if (data.containsKey('started_at')) {
      context.handle(
        _startedAtMeta,
        startedAt.isAcceptableOrUnknown(data['started_at']!, _startedAtMeta),
      );
    } else if (isInserting) {
      context.missing(_startedAtMeta);
    }
    if (data.containsKey('updated_at')) {
      context.handle(
        _updatedAtMeta,
        updatedAt.isAcceptableOrUnknown(data['updated_at']!, _updatedAtMeta),
      );
    }
    if (data.containsKey('title')) {
      context.handle(
        _titleMeta,
        title.isAcceptableOrUnknown(data['title']!, _titleMeta),
      );
    }
    if (data.containsKey('last_message_at')) {
      context.handle(
        _lastMessageAtMeta,
        lastMessageAt.isAcceptableOrUnknown(
          data['last_message_at']!,
          _lastMessageAtMeta,
        ),
      );
    }
    if (data.containsKey('last_message_preview')) {
      context.handle(
        _lastMessagePreviewMeta,
        lastMessagePreview.isAcceptableOrUnknown(
          data['last_message_preview']!,
          _lastMessagePreviewMeta,
        ),
      );
    }
    if (data.containsKey('message_count')) {
      context.handle(
        _messageCountMeta,
        messageCount.isAcceptableOrUnknown(
          data['message_count']!,
          _messageCountMeta,
        ),
      );
    }
    if (data.containsKey('agent_id')) {
      context.handle(
        _agentIdMeta,
        agentId.isAcceptableOrUnknown(data['agent_id']!, _agentIdMeta),
      );
    }
    return context;
  }

  @override
  Set<GeneratedColumn> get $primaryKey => {id};
  @override
  ChatSession map(Map<String, dynamic> data, {String? tablePrefix}) {
    final effectivePrefix = tablePrefix != null ? '$tablePrefix.' : '';
    return ChatSession(
      id: attachedDatabase.typeMapping.read(
        DriftSqlType.string,
        data['${effectivePrefix}id'],
      )!,
      userId: attachedDatabase.typeMapping.read(
        DriftSqlType.string,
        data['${effectivePrefix}user_id'],
      )!,
      startedAt: attachedDatabase.typeMapping.read(
        DriftSqlType.dateTime,
        data['${effectivePrefix}started_at'],
      )!,
      updatedAt: attachedDatabase.typeMapping.read(
        DriftSqlType.dateTime,
        data['${effectivePrefix}updated_at'],
      )!,
      title: attachedDatabase.typeMapping.read(
        DriftSqlType.string,
        data['${effectivePrefix}title'],
      ),
      lastMessageAt: attachedDatabase.typeMapping.read(
        DriftSqlType.dateTime,
        data['${effectivePrefix}last_message_at'],
      ),
      lastMessagePreview: attachedDatabase.typeMapping.read(
        DriftSqlType.string,
        data['${effectivePrefix}last_message_preview'],
      ),
      messageCount: attachedDatabase.typeMapping.read(
        DriftSqlType.int,
        data['${effectivePrefix}message_count'],
      )!,
      agentId: attachedDatabase.typeMapping.read(
        DriftSqlType.string,
        data['${effectivePrefix}agent_id'],
      ),
    );
  }

  @override
  $ChatSessionsTable createAlias(String alias) {
    return $ChatSessionsTable(attachedDatabase, alias);
  }
}

class ChatSession extends DataClass implements Insertable<ChatSession> {
  final String id;
  final String userId;
  final DateTime startedAt;
  final DateTime updatedAt;
  final String? title;
  final DateTime? lastMessageAt;
  final String? lastMessagePreview;
  final int messageCount;
  final String? agentId;
  const ChatSession({
    required this.id,
    required this.userId,
    required this.startedAt,
    required this.updatedAt,
    this.title,
    this.lastMessageAt,
    this.lastMessagePreview,
    required this.messageCount,
    this.agentId,
  });
  @override
  Map<String, Expression> toColumns(bool nullToAbsent) {
    final map = <String, Expression>{};
    map['id'] = Variable<String>(id);
    map['user_id'] = Variable<String>(userId);
    map['started_at'] = Variable<DateTime>(startedAt);
    map['updated_at'] = Variable<DateTime>(updatedAt);
    if (!nullToAbsent || title != null) {
      map['title'] = Variable<String>(title);
    }
    if (!nullToAbsent || lastMessageAt != null) {
      map['last_message_at'] = Variable<DateTime>(lastMessageAt);
    }
    if (!nullToAbsent || lastMessagePreview != null) {
      map['last_message_preview'] = Variable<String>(lastMessagePreview);
    }
    map['message_count'] = Variable<int>(messageCount);
    if (!nullToAbsent || agentId != null) {
      map['agent_id'] = Variable<String>(agentId);
    }
    return map;
  }

  ChatSessionsCompanion toCompanion(bool nullToAbsent) {
    return ChatSessionsCompanion(
      id: Value(id),
      userId: Value(userId),
      startedAt: Value(startedAt),
      updatedAt: Value(updatedAt),
      title: title == null && nullToAbsent
          ? const Value.absent()
          : Value(title),
      lastMessageAt: lastMessageAt == null && nullToAbsent
          ? const Value.absent()
          : Value(lastMessageAt),
      lastMessagePreview: lastMessagePreview == null && nullToAbsent
          ? const Value.absent()
          : Value(lastMessagePreview),
      messageCount: Value(messageCount),
      agentId: agentId == null && nullToAbsent
          ? const Value.absent()
          : Value(agentId),
    );
  }

  factory ChatSession.fromJson(
    Map<String, dynamic> json, {
    ValueSerializer? serializer,
  }) {
    serializer ??= driftRuntimeOptions.defaultSerializer;
    return ChatSession(
      id: serializer.fromJson<String>(json['id']),
      userId: serializer.fromJson<String>(json['userId']),
      startedAt: serializer.fromJson<DateTime>(json['startedAt']),
      updatedAt: serializer.fromJson<DateTime>(json['updatedAt']),
      title: serializer.fromJson<String?>(json['title']),
      lastMessageAt: serializer.fromJson<DateTime?>(json['lastMessageAt']),
      lastMessagePreview: serializer.fromJson<String?>(
        json['lastMessagePreview'],
      ),
      messageCount: serializer.fromJson<int>(json['messageCount']),
      agentId: serializer.fromJson<String?>(json['agentId']),
    );
  }
  @override
  Map<String, dynamic> toJson({ValueSerializer? serializer}) {
    serializer ??= driftRuntimeOptions.defaultSerializer;
    return <String, dynamic>{
      'id': serializer.toJson<String>(id),
      'userId': serializer.toJson<String>(userId),
      'startedAt': serializer.toJson<DateTime>(startedAt),
      'updatedAt': serializer.toJson<DateTime>(updatedAt),
      'title': serializer.toJson<String?>(title),
      'lastMessageAt': serializer.toJson<DateTime?>(lastMessageAt),
      'lastMessagePreview': serializer.toJson<String?>(lastMessagePreview),
      'messageCount': serializer.toJson<int>(messageCount),
      'agentId': serializer.toJson<String?>(agentId),
    };
  }

  ChatSession copyWith({
    String? id,
    String? userId,
    DateTime? startedAt,
    DateTime? updatedAt,
    Value<String?> title = const Value.absent(),
    Value<DateTime?> lastMessageAt = const Value.absent(),
    Value<String?> lastMessagePreview = const Value.absent(),
    int? messageCount,
    Value<String?> agentId = const Value.absent(),
  }) => ChatSession(
    id: id ?? this.id,
    userId: userId ?? this.userId,
    startedAt: startedAt ?? this.startedAt,
    updatedAt: updatedAt ?? this.updatedAt,
    title: title.present ? title.value : this.title,
    lastMessageAt: lastMessageAt.present
        ? lastMessageAt.value
        : this.lastMessageAt,
    lastMessagePreview: lastMessagePreview.present
        ? lastMessagePreview.value
        : this.lastMessagePreview,
    messageCount: messageCount ?? this.messageCount,
    agentId: agentId.present ? agentId.value : this.agentId,
  );
  ChatSession copyWithCompanion(ChatSessionsCompanion data) {
    return ChatSession(
      id: data.id.present ? data.id.value : this.id,
      userId: data.userId.present ? data.userId.value : this.userId,
      startedAt: data.startedAt.present ? data.startedAt.value : this.startedAt,
      updatedAt: data.updatedAt.present ? data.updatedAt.value : this.updatedAt,
      title: data.title.present ? data.title.value : this.title,
      lastMessageAt: data.lastMessageAt.present
          ? data.lastMessageAt.value
          : this.lastMessageAt,
      lastMessagePreview: data.lastMessagePreview.present
          ? data.lastMessagePreview.value
          : this.lastMessagePreview,
      messageCount: data.messageCount.present
          ? data.messageCount.value
          : this.messageCount,
      agentId: data.agentId.present ? data.agentId.value : this.agentId,
    );
  }

  @override
  String toString() {
    return (StringBuffer('ChatSession(')
          ..write('id: $id, ')
          ..write('userId: $userId, ')
          ..write('startedAt: $startedAt, ')
          ..write('updatedAt: $updatedAt, ')
          ..write('title: $title, ')
          ..write('lastMessageAt: $lastMessageAt, ')
          ..write('lastMessagePreview: $lastMessagePreview, ')
          ..write('messageCount: $messageCount, ')
          ..write('agentId: $agentId')
          ..write(')'))
        .toString();
  }

  @override
  int get hashCode => Object.hash(
    id,
    userId,
    startedAt,
    updatedAt,
    title,
    lastMessageAt,
    lastMessagePreview,
    messageCount,
    agentId,
  );
  @override
  bool operator ==(Object other) =>
      identical(this, other) ||
      (other is ChatSession &&
          other.id == this.id &&
          other.userId == this.userId &&
          other.startedAt == this.startedAt &&
          other.updatedAt == this.updatedAt &&
          other.title == this.title &&
          other.lastMessageAt == this.lastMessageAt &&
          other.lastMessagePreview == this.lastMessagePreview &&
          other.messageCount == this.messageCount &&
          other.agentId == this.agentId);
}

class ChatSessionsCompanion extends UpdateCompanion<ChatSession> {
  final Value<String> id;
  final Value<String> userId;
  final Value<DateTime> startedAt;
  final Value<DateTime> updatedAt;
  final Value<String?> title;
  final Value<DateTime?> lastMessageAt;
  final Value<String?> lastMessagePreview;
  final Value<int> messageCount;
  final Value<String?> agentId;
  final Value<int> rowid;
  const ChatSessionsCompanion({
    this.id = const Value.absent(),
    this.userId = const Value.absent(),
    this.startedAt = const Value.absent(),
    this.updatedAt = const Value.absent(),
    this.title = const Value.absent(),
    this.lastMessageAt = const Value.absent(),
    this.lastMessagePreview = const Value.absent(),
    this.messageCount = const Value.absent(),
    this.agentId = const Value.absent(),
    this.rowid = const Value.absent(),
  });
  ChatSessionsCompanion.insert({
    required String id,
    this.userId = const Value.absent(),
    required DateTime startedAt,
    this.updatedAt = const Value.absent(),
    this.title = const Value.absent(),
    this.lastMessageAt = const Value.absent(),
    this.lastMessagePreview = const Value.absent(),
    this.messageCount = const Value.absent(),
    this.agentId = const Value.absent(),
    this.rowid = const Value.absent(),
  }) : id = Value(id),
       startedAt = Value(startedAt);
  static Insertable<ChatSession> custom({
    Expression<String>? id,
    Expression<String>? userId,
    Expression<DateTime>? startedAt,
    Expression<DateTime>? updatedAt,
    Expression<String>? title,
    Expression<DateTime>? lastMessageAt,
    Expression<String>? lastMessagePreview,
    Expression<int>? messageCount,
    Expression<String>? agentId,
    Expression<int>? rowid,
  }) {
    return RawValuesInsertable({
      if (id != null) 'id': id,
      if (userId != null) 'user_id': userId,
      if (startedAt != null) 'started_at': startedAt,
      if (updatedAt != null) 'updated_at': updatedAt,
      if (title != null) 'title': title,
      if (lastMessageAt != null) 'last_message_at': lastMessageAt,
      if (lastMessagePreview != null)
        'last_message_preview': lastMessagePreview,
      if (messageCount != null) 'message_count': messageCount,
      if (agentId != null) 'agent_id': agentId,
      if (rowid != null) 'rowid': rowid,
    });
  }

  ChatSessionsCompanion copyWith({
    Value<String>? id,
    Value<String>? userId,
    Value<DateTime>? startedAt,
    Value<DateTime>? updatedAt,
    Value<String?>? title,
    Value<DateTime?>? lastMessageAt,
    Value<String?>? lastMessagePreview,
    Value<int>? messageCount,
    Value<String?>? agentId,
    Value<int>? rowid,
  }) {
    return ChatSessionsCompanion(
      id: id ?? this.id,
      userId: userId ?? this.userId,
      startedAt: startedAt ?? this.startedAt,
      updatedAt: updatedAt ?? this.updatedAt,
      title: title ?? this.title,
      lastMessageAt: lastMessageAt ?? this.lastMessageAt,
      lastMessagePreview: lastMessagePreview ?? this.lastMessagePreview,
      messageCount: messageCount ?? this.messageCount,
      agentId: agentId ?? this.agentId,
      rowid: rowid ?? this.rowid,
    );
  }

  @override
  Map<String, Expression> toColumns(bool nullToAbsent) {
    final map = <String, Expression>{};
    if (id.present) {
      map['id'] = Variable<String>(id.value);
    }
    if (userId.present) {
      map['user_id'] = Variable<String>(userId.value);
    }
    if (startedAt.present) {
      map['started_at'] = Variable<DateTime>(startedAt.value);
    }
    if (updatedAt.present) {
      map['updated_at'] = Variable<DateTime>(updatedAt.value);
    }
    if (title.present) {
      map['title'] = Variable<String>(title.value);
    }
    if (lastMessageAt.present) {
      map['last_message_at'] = Variable<DateTime>(lastMessageAt.value);
    }
    if (lastMessagePreview.present) {
      map['last_message_preview'] = Variable<String>(lastMessagePreview.value);
    }
    if (messageCount.present) {
      map['message_count'] = Variable<int>(messageCount.value);
    }
    if (agentId.present) {
      map['agent_id'] = Variable<String>(agentId.value);
    }
    if (rowid.present) {
      map['rowid'] = Variable<int>(rowid.value);
    }
    return map;
  }

  @override
  String toString() {
    return (StringBuffer('ChatSessionsCompanion(')
          ..write('id: $id, ')
          ..write('userId: $userId, ')
          ..write('startedAt: $startedAt, ')
          ..write('updatedAt: $updatedAt, ')
          ..write('title: $title, ')
          ..write('lastMessageAt: $lastMessageAt, ')
          ..write('lastMessagePreview: $lastMessagePreview, ')
          ..write('messageCount: $messageCount, ')
          ..write('agentId: $agentId, ')
          ..write('rowid: $rowid')
          ..write(')'))
        .toString();
  }
}

class $ChatMessagesTable extends ChatMessages
    with TableInfo<$ChatMessagesTable, ChatMessage> {
  @override
  final GeneratedDatabase attachedDatabase;
  final String? _alias;
  $ChatMessagesTable(this.attachedDatabase, [this._alias]);
  static const VerificationMeta _idMeta = const VerificationMeta('id');
  @override
  late final GeneratedColumn<String> id = GeneratedColumn<String>(
    'id',
    aliasedName,
    false,
    type: DriftSqlType.string,
    requiredDuringInsert: true,
  );
  static const VerificationMeta _sessionIdMeta = const VerificationMeta(
    'sessionId',
  );
  @override
  late final GeneratedColumn<String> sessionId = GeneratedColumn<String>(
    'session_id',
    aliasedName,
    false,
    type: DriftSqlType.string,
    requiredDuringInsert: true,
    defaultConstraints: GeneratedColumn.constraintIsAlways(
      'REFERENCES chat_sessions (id) ON DELETE CASCADE',
    ),
  );
  static const VerificationMeta _contentMeta = const VerificationMeta(
    'content',
  );
  @override
  late final GeneratedColumn<String> content = GeneratedColumn<String>(
    'content',
    aliasedName,
    false,
    type: DriftSqlType.string,
    requiredDuringInsert: true,
  );
  static const VerificationMeta _isUserMeta = const VerificationMeta('isUser');
  @override
  late final GeneratedColumn<bool> isUser = GeneratedColumn<bool>(
    'is_user',
    aliasedName,
    false,
    type: DriftSqlType.bool,
    requiredDuringInsert: true,
    defaultConstraints: GeneratedColumn.constraintIsAlways(
      'CHECK ("is_user" IN (0, 1))',
    ),
  );
  static const VerificationMeta _channelMeta = const VerificationMeta(
    'channel',
  );
  @override
  late final GeneratedColumn<String> channel = GeneratedColumn<String>(
    'channel',
    aliasedName,
    false,
    type: DriftSqlType.string,
    requiredDuringInsert: true,
  );
  static const VerificationMeta _timestampMeta = const VerificationMeta(
    'timestamp',
  );
  @override
  late final GeneratedColumn<DateTime> timestamp = GeneratedColumn<DateTime>(
    'timestamp',
    aliasedName,
    false,
    type: DriftSqlType.dateTime,
    requiredDuringInsert: true,
  );
  static const VerificationMeta _sequenceMeta = const VerificationMeta(
    'sequence',
  );
  @override
  late final GeneratedColumn<int> sequence = GeneratedColumn<int>(
    'sequence',
    aliasedName,
    false,
    type: DriftSqlType.int,
    requiredDuringInsert: false,
    defaultValue: const Constant(0),
  );
  static const VerificationMeta _feedbackMeta = const VerificationMeta(
    'feedback',
  );
  @override
  late final GeneratedColumn<String> feedback = GeneratedColumn<String>(
    'feedback',
    aliasedName,
    true,
    type: DriftSqlType.string,
    requiredDuringInsert: false,
  );
  static const VerificationMeta _statusMeta = const VerificationMeta('status');
  @override
  late final GeneratedColumn<String> status = GeneratedColumn<String>(
    'status',
    aliasedName,
    true,
    type: DriftSqlType.string,
    requiredDuringInsert: false,
  );
  static const VerificationMeta _errorReasonMeta = const VerificationMeta(
    'errorReason',
  );
  @override
  late final GeneratedColumn<String> errorReason = GeneratedColumn<String>(
    'error_reason',
    aliasedName,
    true,
    type: DriftSqlType.string,
    requiredDuringInsert: false,
  );
  static const VerificationMeta _engagementIdMeta = const VerificationMeta(
    'engagementId',
  );
  @override
  late final GeneratedColumn<String> engagementId = GeneratedColumn<String>(
    'engagement_id',
    aliasedName,
    true,
    type: DriftSqlType.string,
    requiredDuringInsert: false,
  );
  static const VerificationMeta _engagementAgentMeta = const VerificationMeta(
    'engagementAgent',
  );
  @override
  late final GeneratedColumn<String> engagementAgent = GeneratedColumn<String>(
    'engagement_agent',
    aliasedName,
    true,
    type: DriftSqlType.string,
    requiredDuringInsert: false,
  );
  static const VerificationMeta _reminderJsonMeta = const VerificationMeta(
    'reminderJson',
  );
  @override
  late final GeneratedColumn<String> reminderJson = GeneratedColumn<String>(
    'reminder_json',
    aliasedName,
    true,
    type: DriftSqlType.string,
    requiredDuringInsert: false,
  );
  static const VerificationMeta _clarificationJsonMeta = const VerificationMeta(
    'clarificationJson',
  );
  @override
  late final GeneratedColumn<String> clarificationJson =
      GeneratedColumn<String>(
        'clarification_json',
        aliasedName,
        true,
        type: DriftSqlType.string,
        requiredDuringInsert: false,
      );
  static const VerificationMeta _attachmentJsonMeta = const VerificationMeta(
    'attachmentJson',
  );
  @override
  late final GeneratedColumn<String> attachmentJson = GeneratedColumn<String>(
    'attachment_json',
    aliasedName,
    true,
    type: DriftSqlType.string,
    requiredDuringInsert: false,
  );
  static const VerificationMeta _inputMethodMeta = const VerificationMeta(
    'inputMethod',
  );
  @override
  late final GeneratedColumn<String> inputMethod = GeneratedColumn<String>(
    'input_method',
    aliasedName,
    true,
    type: DriftSqlType.string,
    requiredDuringInsert: false,
  );
  @override
  List<GeneratedColumn> get $columns => [
    id,
    sessionId,
    content,
    isUser,
    channel,
    timestamp,
    sequence,
    feedback,
    status,
    errorReason,
    engagementId,
    engagementAgent,
    reminderJson,
    clarificationJson,
    attachmentJson,
    inputMethod,
  ];
  @override
  String get aliasedName => _alias ?? actualTableName;
  @override
  String get actualTableName => $name;
  static const String $name = 'chat_messages';
  @override
  VerificationContext validateIntegrity(
    Insertable<ChatMessage> instance, {
    bool isInserting = false,
  }) {
    final context = VerificationContext();
    final data = instance.toColumns(true);
    if (data.containsKey('id')) {
      context.handle(_idMeta, id.isAcceptableOrUnknown(data['id']!, _idMeta));
    } else if (isInserting) {
      context.missing(_idMeta);
    }
    if (data.containsKey('session_id')) {
      context.handle(
        _sessionIdMeta,
        sessionId.isAcceptableOrUnknown(data['session_id']!, _sessionIdMeta),
      );
    } else if (isInserting) {
      context.missing(_sessionIdMeta);
    }
    if (data.containsKey('content')) {
      context.handle(
        _contentMeta,
        content.isAcceptableOrUnknown(data['content']!, _contentMeta),
      );
    } else if (isInserting) {
      context.missing(_contentMeta);
    }
    if (data.containsKey('is_user')) {
      context.handle(
        _isUserMeta,
        isUser.isAcceptableOrUnknown(data['is_user']!, _isUserMeta),
      );
    } else if (isInserting) {
      context.missing(_isUserMeta);
    }
    if (data.containsKey('channel')) {
      context.handle(
        _channelMeta,
        channel.isAcceptableOrUnknown(data['channel']!, _channelMeta),
      );
    } else if (isInserting) {
      context.missing(_channelMeta);
    }
    if (data.containsKey('timestamp')) {
      context.handle(
        _timestampMeta,
        timestamp.isAcceptableOrUnknown(data['timestamp']!, _timestampMeta),
      );
    } else if (isInserting) {
      context.missing(_timestampMeta);
    }
    if (data.containsKey('sequence')) {
      context.handle(
        _sequenceMeta,
        sequence.isAcceptableOrUnknown(data['sequence']!, _sequenceMeta),
      );
    }
    if (data.containsKey('feedback')) {
      context.handle(
        _feedbackMeta,
        feedback.isAcceptableOrUnknown(data['feedback']!, _feedbackMeta),
      );
    }
    if (data.containsKey('status')) {
      context.handle(
        _statusMeta,
        status.isAcceptableOrUnknown(data['status']!, _statusMeta),
      );
    }
    if (data.containsKey('error_reason')) {
      context.handle(
        _errorReasonMeta,
        errorReason.isAcceptableOrUnknown(
          data['error_reason']!,
          _errorReasonMeta,
        ),
      );
    }
    if (data.containsKey('engagement_id')) {
      context.handle(
        _engagementIdMeta,
        engagementId.isAcceptableOrUnknown(
          data['engagement_id']!,
          _engagementIdMeta,
        ),
      );
    }
    if (data.containsKey('engagement_agent')) {
      context.handle(
        _engagementAgentMeta,
        engagementAgent.isAcceptableOrUnknown(
          data['engagement_agent']!,
          _engagementAgentMeta,
        ),
      );
    }
    if (data.containsKey('reminder_json')) {
      context.handle(
        _reminderJsonMeta,
        reminderJson.isAcceptableOrUnknown(
          data['reminder_json']!,
          _reminderJsonMeta,
        ),
      );
    }
    if (data.containsKey('clarification_json')) {
      context.handle(
        _clarificationJsonMeta,
        clarificationJson.isAcceptableOrUnknown(
          data['clarification_json']!,
          _clarificationJsonMeta,
        ),
      );
    }
    if (data.containsKey('attachment_json')) {
      context.handle(
        _attachmentJsonMeta,
        attachmentJson.isAcceptableOrUnknown(
          data['attachment_json']!,
          _attachmentJsonMeta,
        ),
      );
    }
    if (data.containsKey('input_method')) {
      context.handle(
        _inputMethodMeta,
        inputMethod.isAcceptableOrUnknown(
          data['input_method']!,
          _inputMethodMeta,
        ),
      );
    }
    return context;
  }

  @override
  Set<GeneratedColumn> get $primaryKey => {id};
  @override
  ChatMessage map(Map<String, dynamic> data, {String? tablePrefix}) {
    final effectivePrefix = tablePrefix != null ? '$tablePrefix.' : '';
    return ChatMessage(
      id: attachedDatabase.typeMapping.read(
        DriftSqlType.string,
        data['${effectivePrefix}id'],
      )!,
      sessionId: attachedDatabase.typeMapping.read(
        DriftSqlType.string,
        data['${effectivePrefix}session_id'],
      )!,
      content: attachedDatabase.typeMapping.read(
        DriftSqlType.string,
        data['${effectivePrefix}content'],
      )!,
      isUser: attachedDatabase.typeMapping.read(
        DriftSqlType.bool,
        data['${effectivePrefix}is_user'],
      )!,
      channel: attachedDatabase.typeMapping.read(
        DriftSqlType.string,
        data['${effectivePrefix}channel'],
      )!,
      timestamp: attachedDatabase.typeMapping.read(
        DriftSqlType.dateTime,
        data['${effectivePrefix}timestamp'],
      )!,
      sequence: attachedDatabase.typeMapping.read(
        DriftSqlType.int,
        data['${effectivePrefix}sequence'],
      )!,
      feedback: attachedDatabase.typeMapping.read(
        DriftSqlType.string,
        data['${effectivePrefix}feedback'],
      ),
      status: attachedDatabase.typeMapping.read(
        DriftSqlType.string,
        data['${effectivePrefix}status'],
      ),
      errorReason: attachedDatabase.typeMapping.read(
        DriftSqlType.string,
        data['${effectivePrefix}error_reason'],
      ),
      engagementId: attachedDatabase.typeMapping.read(
        DriftSqlType.string,
        data['${effectivePrefix}engagement_id'],
      ),
      engagementAgent: attachedDatabase.typeMapping.read(
        DriftSqlType.string,
        data['${effectivePrefix}engagement_agent'],
      ),
      reminderJson: attachedDatabase.typeMapping.read(
        DriftSqlType.string,
        data['${effectivePrefix}reminder_json'],
      ),
      clarificationJson: attachedDatabase.typeMapping.read(
        DriftSqlType.string,
        data['${effectivePrefix}clarification_json'],
      ),
      attachmentJson: attachedDatabase.typeMapping.read(
        DriftSqlType.string,
        data['${effectivePrefix}attachment_json'],
      ),
      inputMethod: attachedDatabase.typeMapping.read(
        DriftSqlType.string,
        data['${effectivePrefix}input_method'],
      ),
    );
  }

  @override
  $ChatMessagesTable createAlias(String alias) {
    return $ChatMessagesTable(attachedDatabase, alias);
  }
}

class ChatMessage extends DataClass implements Insertable<ChatMessage> {
  final String id;
  final String sessionId;
  final String content;
  final bool isUser;
  final String channel;
  final DateTime timestamp;
  final int sequence;
  final String? feedback;
  final String? status;
  final String? errorReason;
  final String? engagementId;
  final String? engagementAgent;
  final String? reminderJson;
  final String? clarificationJson;
  final String? attachmentJson;
  final String? inputMethod;
  const ChatMessage({
    required this.id,
    required this.sessionId,
    required this.content,
    required this.isUser,
    required this.channel,
    required this.timestamp,
    required this.sequence,
    this.feedback,
    this.status,
    this.errorReason,
    this.engagementId,
    this.engagementAgent,
    this.reminderJson,
    this.clarificationJson,
    this.attachmentJson,
    this.inputMethod,
  });
  @override
  Map<String, Expression> toColumns(bool nullToAbsent) {
    final map = <String, Expression>{};
    map['id'] = Variable<String>(id);
    map['session_id'] = Variable<String>(sessionId);
    map['content'] = Variable<String>(content);
    map['is_user'] = Variable<bool>(isUser);
    map['channel'] = Variable<String>(channel);
    map['timestamp'] = Variable<DateTime>(timestamp);
    map['sequence'] = Variable<int>(sequence);
    if (!nullToAbsent || feedback != null) {
      map['feedback'] = Variable<String>(feedback);
    }
    if (!nullToAbsent || status != null) {
      map['status'] = Variable<String>(status);
    }
    if (!nullToAbsent || errorReason != null) {
      map['error_reason'] = Variable<String>(errorReason);
    }
    if (!nullToAbsent || engagementId != null) {
      map['engagement_id'] = Variable<String>(engagementId);
    }
    if (!nullToAbsent || engagementAgent != null) {
      map['engagement_agent'] = Variable<String>(engagementAgent);
    }
    if (!nullToAbsent || reminderJson != null) {
      map['reminder_json'] = Variable<String>(reminderJson);
    }
    if (!nullToAbsent || clarificationJson != null) {
      map['clarification_json'] = Variable<String>(clarificationJson);
    }
    if (!nullToAbsent || attachmentJson != null) {
      map['attachment_json'] = Variable<String>(attachmentJson);
    }
    if (!nullToAbsent || inputMethod != null) {
      map['input_method'] = Variable<String>(inputMethod);
    }
    return map;
  }

  ChatMessagesCompanion toCompanion(bool nullToAbsent) {
    return ChatMessagesCompanion(
      id: Value(id),
      sessionId: Value(sessionId),
      content: Value(content),
      isUser: Value(isUser),
      channel: Value(channel),
      timestamp: Value(timestamp),
      sequence: Value(sequence),
      feedback: feedback == null && nullToAbsent
          ? const Value.absent()
          : Value(feedback),
      status: status == null && nullToAbsent
          ? const Value.absent()
          : Value(status),
      errorReason: errorReason == null && nullToAbsent
          ? const Value.absent()
          : Value(errorReason),
      engagementId: engagementId == null && nullToAbsent
          ? const Value.absent()
          : Value(engagementId),
      engagementAgent: engagementAgent == null && nullToAbsent
          ? const Value.absent()
          : Value(engagementAgent),
      reminderJson: reminderJson == null && nullToAbsent
          ? const Value.absent()
          : Value(reminderJson),
      clarificationJson: clarificationJson == null && nullToAbsent
          ? const Value.absent()
          : Value(clarificationJson),
      attachmentJson: attachmentJson == null && nullToAbsent
          ? const Value.absent()
          : Value(attachmentJson),
      inputMethod: inputMethod == null && nullToAbsent
          ? const Value.absent()
          : Value(inputMethod),
    );
  }

  factory ChatMessage.fromJson(
    Map<String, dynamic> json, {
    ValueSerializer? serializer,
  }) {
    serializer ??= driftRuntimeOptions.defaultSerializer;
    return ChatMessage(
      id: serializer.fromJson<String>(json['id']),
      sessionId: serializer.fromJson<String>(json['sessionId']),
      content: serializer.fromJson<String>(json['content']),
      isUser: serializer.fromJson<bool>(json['isUser']),
      channel: serializer.fromJson<String>(json['channel']),
      timestamp: serializer.fromJson<DateTime>(json['timestamp']),
      sequence: serializer.fromJson<int>(json['sequence']),
      feedback: serializer.fromJson<String?>(json['feedback']),
      status: serializer.fromJson<String?>(json['status']),
      errorReason: serializer.fromJson<String?>(json['errorReason']),
      engagementId: serializer.fromJson<String?>(json['engagementId']),
      engagementAgent: serializer.fromJson<String?>(json['engagementAgent']),
      reminderJson: serializer.fromJson<String?>(json['reminderJson']),
      clarificationJson: serializer.fromJson<String?>(
        json['clarificationJson'],
      ),
      attachmentJson: serializer.fromJson<String?>(json['attachmentJson']),
      inputMethod: serializer.fromJson<String?>(json['inputMethod']),
    );
  }
  @override
  Map<String, dynamic> toJson({ValueSerializer? serializer}) {
    serializer ??= driftRuntimeOptions.defaultSerializer;
    return <String, dynamic>{
      'id': serializer.toJson<String>(id),
      'sessionId': serializer.toJson<String>(sessionId),
      'content': serializer.toJson<String>(content),
      'isUser': serializer.toJson<bool>(isUser),
      'channel': serializer.toJson<String>(channel),
      'timestamp': serializer.toJson<DateTime>(timestamp),
      'sequence': serializer.toJson<int>(sequence),
      'feedback': serializer.toJson<String?>(feedback),
      'status': serializer.toJson<String?>(status),
      'errorReason': serializer.toJson<String?>(errorReason),
      'engagementId': serializer.toJson<String?>(engagementId),
      'engagementAgent': serializer.toJson<String?>(engagementAgent),
      'reminderJson': serializer.toJson<String?>(reminderJson),
      'clarificationJson': serializer.toJson<String?>(clarificationJson),
      'attachmentJson': serializer.toJson<String?>(attachmentJson),
      'inputMethod': serializer.toJson<String?>(inputMethod),
    };
  }

  ChatMessage copyWith({
    String? id,
    String? sessionId,
    String? content,
    bool? isUser,
    String? channel,
    DateTime? timestamp,
    int? sequence,
    Value<String?> feedback = const Value.absent(),
    Value<String?> status = const Value.absent(),
    Value<String?> errorReason = const Value.absent(),
    Value<String?> engagementId = const Value.absent(),
    Value<String?> engagementAgent = const Value.absent(),
    Value<String?> reminderJson = const Value.absent(),
    Value<String?> clarificationJson = const Value.absent(),
    Value<String?> attachmentJson = const Value.absent(),
    Value<String?> inputMethod = const Value.absent(),
  }) => ChatMessage(
    id: id ?? this.id,
    sessionId: sessionId ?? this.sessionId,
    content: content ?? this.content,
    isUser: isUser ?? this.isUser,
    channel: channel ?? this.channel,
    timestamp: timestamp ?? this.timestamp,
    sequence: sequence ?? this.sequence,
    feedback: feedback.present ? feedback.value : this.feedback,
    status: status.present ? status.value : this.status,
    errorReason: errorReason.present ? errorReason.value : this.errorReason,
    engagementId: engagementId.present ? engagementId.value : this.engagementId,
    engagementAgent: engagementAgent.present
        ? engagementAgent.value
        : this.engagementAgent,
    reminderJson: reminderJson.present ? reminderJson.value : this.reminderJson,
    clarificationJson: clarificationJson.present
        ? clarificationJson.value
        : this.clarificationJson,
    attachmentJson: attachmentJson.present
        ? attachmentJson.value
        : this.attachmentJson,
    inputMethod: inputMethod.present ? inputMethod.value : this.inputMethod,
  );
  ChatMessage copyWithCompanion(ChatMessagesCompanion data) {
    return ChatMessage(
      id: data.id.present ? data.id.value : this.id,
      sessionId: data.sessionId.present ? data.sessionId.value : this.sessionId,
      content: data.content.present ? data.content.value : this.content,
      isUser: data.isUser.present ? data.isUser.value : this.isUser,
      channel: data.channel.present ? data.channel.value : this.channel,
      timestamp: data.timestamp.present ? data.timestamp.value : this.timestamp,
      sequence: data.sequence.present ? data.sequence.value : this.sequence,
      feedback: data.feedback.present ? data.feedback.value : this.feedback,
      status: data.status.present ? data.status.value : this.status,
      errorReason: data.errorReason.present
          ? data.errorReason.value
          : this.errorReason,
      engagementId: data.engagementId.present
          ? data.engagementId.value
          : this.engagementId,
      engagementAgent: data.engagementAgent.present
          ? data.engagementAgent.value
          : this.engagementAgent,
      reminderJson: data.reminderJson.present
          ? data.reminderJson.value
          : this.reminderJson,
      clarificationJson: data.clarificationJson.present
          ? data.clarificationJson.value
          : this.clarificationJson,
      attachmentJson: data.attachmentJson.present
          ? data.attachmentJson.value
          : this.attachmentJson,
      inputMethod: data.inputMethod.present
          ? data.inputMethod.value
          : this.inputMethod,
    );
  }

  @override
  String toString() {
    return (StringBuffer('ChatMessage(')
          ..write('id: $id, ')
          ..write('sessionId: $sessionId, ')
          ..write('content: $content, ')
          ..write('isUser: $isUser, ')
          ..write('channel: $channel, ')
          ..write('timestamp: $timestamp, ')
          ..write('sequence: $sequence, ')
          ..write('feedback: $feedback, ')
          ..write('status: $status, ')
          ..write('errorReason: $errorReason, ')
          ..write('engagementId: $engagementId, ')
          ..write('engagementAgent: $engagementAgent, ')
          ..write('reminderJson: $reminderJson, ')
          ..write('clarificationJson: $clarificationJson, ')
          ..write('attachmentJson: $attachmentJson, ')
          ..write('inputMethod: $inputMethod')
          ..write(')'))
        .toString();
  }

  @override
  int get hashCode => Object.hash(
    id,
    sessionId,
    content,
    isUser,
    channel,
    timestamp,
    sequence,
    feedback,
    status,
    errorReason,
    engagementId,
    engagementAgent,
    reminderJson,
    clarificationJson,
    attachmentJson,
    inputMethod,
  );
  @override
  bool operator ==(Object other) =>
      identical(this, other) ||
      (other is ChatMessage &&
          other.id == this.id &&
          other.sessionId == this.sessionId &&
          other.content == this.content &&
          other.isUser == this.isUser &&
          other.channel == this.channel &&
          other.timestamp == this.timestamp &&
          other.sequence == this.sequence &&
          other.feedback == this.feedback &&
          other.status == this.status &&
          other.errorReason == this.errorReason &&
          other.engagementId == this.engagementId &&
          other.engagementAgent == this.engagementAgent &&
          other.reminderJson == this.reminderJson &&
          other.clarificationJson == this.clarificationJson &&
          other.attachmentJson == this.attachmentJson &&
          other.inputMethod == this.inputMethod);
}

class ChatMessagesCompanion extends UpdateCompanion<ChatMessage> {
  final Value<String> id;
  final Value<String> sessionId;
  final Value<String> content;
  final Value<bool> isUser;
  final Value<String> channel;
  final Value<DateTime> timestamp;
  final Value<int> sequence;
  final Value<String?> feedback;
  final Value<String?> status;
  final Value<String?> errorReason;
  final Value<String?> engagementId;
  final Value<String?> engagementAgent;
  final Value<String?> reminderJson;
  final Value<String?> clarificationJson;
  final Value<String?> attachmentJson;
  final Value<String?> inputMethod;
  final Value<int> rowid;
  const ChatMessagesCompanion({
    this.id = const Value.absent(),
    this.sessionId = const Value.absent(),
    this.content = const Value.absent(),
    this.isUser = const Value.absent(),
    this.channel = const Value.absent(),
    this.timestamp = const Value.absent(),
    this.sequence = const Value.absent(),
    this.feedback = const Value.absent(),
    this.status = const Value.absent(),
    this.errorReason = const Value.absent(),
    this.engagementId = const Value.absent(),
    this.engagementAgent = const Value.absent(),
    this.reminderJson = const Value.absent(),
    this.clarificationJson = const Value.absent(),
    this.attachmentJson = const Value.absent(),
    this.inputMethod = const Value.absent(),
    this.rowid = const Value.absent(),
  });
  ChatMessagesCompanion.insert({
    required String id,
    required String sessionId,
    required String content,
    required bool isUser,
    required String channel,
    required DateTime timestamp,
    this.sequence = const Value.absent(),
    this.feedback = const Value.absent(),
    this.status = const Value.absent(),
    this.errorReason = const Value.absent(),
    this.engagementId = const Value.absent(),
    this.engagementAgent = const Value.absent(),
    this.reminderJson = const Value.absent(),
    this.clarificationJson = const Value.absent(),
    this.attachmentJson = const Value.absent(),
    this.inputMethod = const Value.absent(),
    this.rowid = const Value.absent(),
  }) : id = Value(id),
       sessionId = Value(sessionId),
       content = Value(content),
       isUser = Value(isUser),
       channel = Value(channel),
       timestamp = Value(timestamp);
  static Insertable<ChatMessage> custom({
    Expression<String>? id,
    Expression<String>? sessionId,
    Expression<String>? content,
    Expression<bool>? isUser,
    Expression<String>? channel,
    Expression<DateTime>? timestamp,
    Expression<int>? sequence,
    Expression<String>? feedback,
    Expression<String>? status,
    Expression<String>? errorReason,
    Expression<String>? engagementId,
    Expression<String>? engagementAgent,
    Expression<String>? reminderJson,
    Expression<String>? clarificationJson,
    Expression<String>? attachmentJson,
    Expression<String>? inputMethod,
    Expression<int>? rowid,
  }) {
    return RawValuesInsertable({
      if (id != null) 'id': id,
      if (sessionId != null) 'session_id': sessionId,
      if (content != null) 'content': content,
      if (isUser != null) 'is_user': isUser,
      if (channel != null) 'channel': channel,
      if (timestamp != null) 'timestamp': timestamp,
      if (sequence != null) 'sequence': sequence,
      if (feedback != null) 'feedback': feedback,
      if (status != null) 'status': status,
      if (errorReason != null) 'error_reason': errorReason,
      if (engagementId != null) 'engagement_id': engagementId,
      if (engagementAgent != null) 'engagement_agent': engagementAgent,
      if (reminderJson != null) 'reminder_json': reminderJson,
      if (clarificationJson != null) 'clarification_json': clarificationJson,
      if (attachmentJson != null) 'attachment_json': attachmentJson,
      if (inputMethod != null) 'input_method': inputMethod,
      if (rowid != null) 'rowid': rowid,
    });
  }

  ChatMessagesCompanion copyWith({
    Value<String>? id,
    Value<String>? sessionId,
    Value<String>? content,
    Value<bool>? isUser,
    Value<String>? channel,
    Value<DateTime>? timestamp,
    Value<int>? sequence,
    Value<String?>? feedback,
    Value<String?>? status,
    Value<String?>? errorReason,
    Value<String?>? engagementId,
    Value<String?>? engagementAgent,
    Value<String?>? reminderJson,
    Value<String?>? clarificationJson,
    Value<String?>? attachmentJson,
    Value<String?>? inputMethod,
    Value<int>? rowid,
  }) {
    return ChatMessagesCompanion(
      id: id ?? this.id,
      sessionId: sessionId ?? this.sessionId,
      content: content ?? this.content,
      isUser: isUser ?? this.isUser,
      channel: channel ?? this.channel,
      timestamp: timestamp ?? this.timestamp,
      sequence: sequence ?? this.sequence,
      feedback: feedback ?? this.feedback,
      status: status ?? this.status,
      errorReason: errorReason ?? this.errorReason,
      engagementId: engagementId ?? this.engagementId,
      engagementAgent: engagementAgent ?? this.engagementAgent,
      reminderJson: reminderJson ?? this.reminderJson,
      clarificationJson: clarificationJson ?? this.clarificationJson,
      attachmentJson: attachmentJson ?? this.attachmentJson,
      inputMethod: inputMethod ?? this.inputMethod,
      rowid: rowid ?? this.rowid,
    );
  }

  @override
  Map<String, Expression> toColumns(bool nullToAbsent) {
    final map = <String, Expression>{};
    if (id.present) {
      map['id'] = Variable<String>(id.value);
    }
    if (sessionId.present) {
      map['session_id'] = Variable<String>(sessionId.value);
    }
    if (content.present) {
      map['content'] = Variable<String>(content.value);
    }
    if (isUser.present) {
      map['is_user'] = Variable<bool>(isUser.value);
    }
    if (channel.present) {
      map['channel'] = Variable<String>(channel.value);
    }
    if (timestamp.present) {
      map['timestamp'] = Variable<DateTime>(timestamp.value);
    }
    if (sequence.present) {
      map['sequence'] = Variable<int>(sequence.value);
    }
    if (feedback.present) {
      map['feedback'] = Variable<String>(feedback.value);
    }
    if (status.present) {
      map['status'] = Variable<String>(status.value);
    }
    if (errorReason.present) {
      map['error_reason'] = Variable<String>(errorReason.value);
    }
    if (engagementId.present) {
      map['engagement_id'] = Variable<String>(engagementId.value);
    }
    if (engagementAgent.present) {
      map['engagement_agent'] = Variable<String>(engagementAgent.value);
    }
    if (reminderJson.present) {
      map['reminder_json'] = Variable<String>(reminderJson.value);
    }
    if (clarificationJson.present) {
      map['clarification_json'] = Variable<String>(clarificationJson.value);
    }
    if (attachmentJson.present) {
      map['attachment_json'] = Variable<String>(attachmentJson.value);
    }
    if (inputMethod.present) {
      map['input_method'] = Variable<String>(inputMethod.value);
    }
    if (rowid.present) {
      map['rowid'] = Variable<int>(rowid.value);
    }
    return map;
  }

  @override
  String toString() {
    return (StringBuffer('ChatMessagesCompanion(')
          ..write('id: $id, ')
          ..write('sessionId: $sessionId, ')
          ..write('content: $content, ')
          ..write('isUser: $isUser, ')
          ..write('channel: $channel, ')
          ..write('timestamp: $timestamp, ')
          ..write('sequence: $sequence, ')
          ..write('feedback: $feedback, ')
          ..write('status: $status, ')
          ..write('errorReason: $errorReason, ')
          ..write('engagementId: $engagementId, ')
          ..write('engagementAgent: $engagementAgent, ')
          ..write('reminderJson: $reminderJson, ')
          ..write('clarificationJson: $clarificationJson, ')
          ..write('attachmentJson: $attachmentJson, ')
          ..write('inputMethod: $inputMethod, ')
          ..write('rowid: $rowid')
          ..write(')'))
        .toString();
  }
}

class $ChatSyncJobsTable extends ChatSyncJobs
    with TableInfo<$ChatSyncJobsTable, ChatSyncJob> {
  @override
  final GeneratedDatabase attachedDatabase;
  final String? _alias;
  $ChatSyncJobsTable(this.attachedDatabase, [this._alias]);
  static const VerificationMeta _idMeta = const VerificationMeta('id');
  @override
  late final GeneratedColumn<int> id = GeneratedColumn<int>(
    'id',
    aliasedName,
    false,
    hasAutoIncrement: true,
    type: DriftSqlType.int,
    requiredDuringInsert: false,
    defaultConstraints: GeneratedColumn.constraintIsAlways(
      'PRIMARY KEY AUTOINCREMENT',
    ),
  );
  static const VerificationMeta _userIdMeta = const VerificationMeta('userId');
  @override
  late final GeneratedColumn<String> userId = GeneratedColumn<String>(
    'user_id',
    aliasedName,
    false,
    type: DriftSqlType.string,
    requiredDuringInsert: true,
  );
  static const VerificationMeta _sessionIdMeta = const VerificationMeta(
    'sessionId',
  );
  @override
  late final GeneratedColumn<String> sessionId = GeneratedColumn<String>(
    'session_id',
    aliasedName,
    false,
    type: DriftSqlType.string,
    requiredDuringInsert: true,
  );
  static const VerificationMeta _messageIdMeta = const VerificationMeta(
    'messageId',
  );
  @override
  late final GeneratedColumn<String> messageId = GeneratedColumn<String>(
    'message_id',
    aliasedName,
    true,
    type: DriftSqlType.string,
    requiredDuringInsert: false,
  );
  static const VerificationMeta _jobTypeMeta = const VerificationMeta(
    'jobType',
  );
  @override
  late final GeneratedColumn<String> jobType = GeneratedColumn<String>(
    'job_type',
    aliasedName,
    false,
    type: DriftSqlType.string,
    requiredDuringInsert: true,
  );
  static const VerificationMeta _createdAtMeta = const VerificationMeta(
    'createdAt',
  );
  @override
  late final GeneratedColumn<DateTime> createdAt = GeneratedColumn<DateTime>(
    'created_at',
    aliasedName,
    false,
    type: DriftSqlType.dateTime,
    requiredDuringInsert: false,
    defaultValue: currentDateAndTime,
  );
  static const VerificationMeta _nextAttemptAtMeta = const VerificationMeta(
    'nextAttemptAt',
  );
  @override
  late final GeneratedColumn<DateTime> nextAttemptAt =
      GeneratedColumn<DateTime>(
        'next_attempt_at',
        aliasedName,
        false,
        type: DriftSqlType.dateTime,
        requiredDuringInsert: false,
        defaultValue: currentDateAndTime,
      );
  static const VerificationMeta _attemptCountMeta = const VerificationMeta(
    'attemptCount',
  );
  @override
  late final GeneratedColumn<int> attemptCount = GeneratedColumn<int>(
    'attempt_count',
    aliasedName,
    false,
    type: DriftSqlType.int,
    requiredDuringInsert: false,
    defaultValue: const Constant(0),
  );
  static const VerificationMeta _lastErrorMeta = const VerificationMeta(
    'lastError',
  );
  @override
  late final GeneratedColumn<String> lastError = GeneratedColumn<String>(
    'last_error',
    aliasedName,
    true,
    type: DriftSqlType.string,
    requiredDuringInsert: false,
  );
  @override
  List<GeneratedColumn> get $columns => [
    id,
    userId,
    sessionId,
    messageId,
    jobType,
    createdAt,
    nextAttemptAt,
    attemptCount,
    lastError,
  ];
  @override
  String get aliasedName => _alias ?? actualTableName;
  @override
  String get actualTableName => $name;
  static const String $name = 'chat_sync_jobs';
  @override
  VerificationContext validateIntegrity(
    Insertable<ChatSyncJob> instance, {
    bool isInserting = false,
  }) {
    final context = VerificationContext();
    final data = instance.toColumns(true);
    if (data.containsKey('id')) {
      context.handle(_idMeta, id.isAcceptableOrUnknown(data['id']!, _idMeta));
    }
    if (data.containsKey('user_id')) {
      context.handle(
        _userIdMeta,
        userId.isAcceptableOrUnknown(data['user_id']!, _userIdMeta),
      );
    } else if (isInserting) {
      context.missing(_userIdMeta);
    }
    if (data.containsKey('session_id')) {
      context.handle(
        _sessionIdMeta,
        sessionId.isAcceptableOrUnknown(data['session_id']!, _sessionIdMeta),
      );
    } else if (isInserting) {
      context.missing(_sessionIdMeta);
    }
    if (data.containsKey('message_id')) {
      context.handle(
        _messageIdMeta,
        messageId.isAcceptableOrUnknown(data['message_id']!, _messageIdMeta),
      );
    }
    if (data.containsKey('job_type')) {
      context.handle(
        _jobTypeMeta,
        jobType.isAcceptableOrUnknown(data['job_type']!, _jobTypeMeta),
      );
    } else if (isInserting) {
      context.missing(_jobTypeMeta);
    }
    if (data.containsKey('created_at')) {
      context.handle(
        _createdAtMeta,
        createdAt.isAcceptableOrUnknown(data['created_at']!, _createdAtMeta),
      );
    }
    if (data.containsKey('next_attempt_at')) {
      context.handle(
        _nextAttemptAtMeta,
        nextAttemptAt.isAcceptableOrUnknown(
          data['next_attempt_at']!,
          _nextAttemptAtMeta,
        ),
      );
    }
    if (data.containsKey('attempt_count')) {
      context.handle(
        _attemptCountMeta,
        attemptCount.isAcceptableOrUnknown(
          data['attempt_count']!,
          _attemptCountMeta,
        ),
      );
    }
    if (data.containsKey('last_error')) {
      context.handle(
        _lastErrorMeta,
        lastError.isAcceptableOrUnknown(data['last_error']!, _lastErrorMeta),
      );
    }
    return context;
  }

  @override
  Set<GeneratedColumn> get $primaryKey => {id};
  @override
  ChatSyncJob map(Map<String, dynamic> data, {String? tablePrefix}) {
    final effectivePrefix = tablePrefix != null ? '$tablePrefix.' : '';
    return ChatSyncJob(
      id: attachedDatabase.typeMapping.read(
        DriftSqlType.int,
        data['${effectivePrefix}id'],
      )!,
      userId: attachedDatabase.typeMapping.read(
        DriftSqlType.string,
        data['${effectivePrefix}user_id'],
      )!,
      sessionId: attachedDatabase.typeMapping.read(
        DriftSqlType.string,
        data['${effectivePrefix}session_id'],
      )!,
      messageId: attachedDatabase.typeMapping.read(
        DriftSqlType.string,
        data['${effectivePrefix}message_id'],
      ),
      jobType: attachedDatabase.typeMapping.read(
        DriftSqlType.string,
        data['${effectivePrefix}job_type'],
      )!,
      createdAt: attachedDatabase.typeMapping.read(
        DriftSqlType.dateTime,
        data['${effectivePrefix}created_at'],
      )!,
      nextAttemptAt: attachedDatabase.typeMapping.read(
        DriftSqlType.dateTime,
        data['${effectivePrefix}next_attempt_at'],
      )!,
      attemptCount: attachedDatabase.typeMapping.read(
        DriftSqlType.int,
        data['${effectivePrefix}attempt_count'],
      )!,
      lastError: attachedDatabase.typeMapping.read(
        DriftSqlType.string,
        data['${effectivePrefix}last_error'],
      ),
    );
  }

  @override
  $ChatSyncJobsTable createAlias(String alias) {
    return $ChatSyncJobsTable(attachedDatabase, alias);
  }
}

class ChatSyncJob extends DataClass implements Insertable<ChatSyncJob> {
  final int id;
  final String userId;
  final String sessionId;
  final String? messageId;
  final String jobType;
  final DateTime createdAt;
  final DateTime nextAttemptAt;
  final int attemptCount;
  final String? lastError;
  const ChatSyncJob({
    required this.id,
    required this.userId,
    required this.sessionId,
    this.messageId,
    required this.jobType,
    required this.createdAt,
    required this.nextAttemptAt,
    required this.attemptCount,
    this.lastError,
  });
  @override
  Map<String, Expression> toColumns(bool nullToAbsent) {
    final map = <String, Expression>{};
    map['id'] = Variable<int>(id);
    map['user_id'] = Variable<String>(userId);
    map['session_id'] = Variable<String>(sessionId);
    if (!nullToAbsent || messageId != null) {
      map['message_id'] = Variable<String>(messageId);
    }
    map['job_type'] = Variable<String>(jobType);
    map['created_at'] = Variable<DateTime>(createdAt);
    map['next_attempt_at'] = Variable<DateTime>(nextAttemptAt);
    map['attempt_count'] = Variable<int>(attemptCount);
    if (!nullToAbsent || lastError != null) {
      map['last_error'] = Variable<String>(lastError);
    }
    return map;
  }

  ChatSyncJobsCompanion toCompanion(bool nullToAbsent) {
    return ChatSyncJobsCompanion(
      id: Value(id),
      userId: Value(userId),
      sessionId: Value(sessionId),
      messageId: messageId == null && nullToAbsent
          ? const Value.absent()
          : Value(messageId),
      jobType: Value(jobType),
      createdAt: Value(createdAt),
      nextAttemptAt: Value(nextAttemptAt),
      attemptCount: Value(attemptCount),
      lastError: lastError == null && nullToAbsent
          ? const Value.absent()
          : Value(lastError),
    );
  }

  factory ChatSyncJob.fromJson(
    Map<String, dynamic> json, {
    ValueSerializer? serializer,
  }) {
    serializer ??= driftRuntimeOptions.defaultSerializer;
    return ChatSyncJob(
      id: serializer.fromJson<int>(json['id']),
      userId: serializer.fromJson<String>(json['userId']),
      sessionId: serializer.fromJson<String>(json['sessionId']),
      messageId: serializer.fromJson<String?>(json['messageId']),
      jobType: serializer.fromJson<String>(json['jobType']),
      createdAt: serializer.fromJson<DateTime>(json['createdAt']),
      nextAttemptAt: serializer.fromJson<DateTime>(json['nextAttemptAt']),
      attemptCount: serializer.fromJson<int>(json['attemptCount']),
      lastError: serializer.fromJson<String?>(json['lastError']),
    );
  }
  @override
  Map<String, dynamic> toJson({ValueSerializer? serializer}) {
    serializer ??= driftRuntimeOptions.defaultSerializer;
    return <String, dynamic>{
      'id': serializer.toJson<int>(id),
      'userId': serializer.toJson<String>(userId),
      'sessionId': serializer.toJson<String>(sessionId),
      'messageId': serializer.toJson<String?>(messageId),
      'jobType': serializer.toJson<String>(jobType),
      'createdAt': serializer.toJson<DateTime>(createdAt),
      'nextAttemptAt': serializer.toJson<DateTime>(nextAttemptAt),
      'attemptCount': serializer.toJson<int>(attemptCount),
      'lastError': serializer.toJson<String?>(lastError),
    };
  }

  ChatSyncJob copyWith({
    int? id,
    String? userId,
    String? sessionId,
    Value<String?> messageId = const Value.absent(),
    String? jobType,
    DateTime? createdAt,
    DateTime? nextAttemptAt,
    int? attemptCount,
    Value<String?> lastError = const Value.absent(),
  }) => ChatSyncJob(
    id: id ?? this.id,
    userId: userId ?? this.userId,
    sessionId: sessionId ?? this.sessionId,
    messageId: messageId.present ? messageId.value : this.messageId,
    jobType: jobType ?? this.jobType,
    createdAt: createdAt ?? this.createdAt,
    nextAttemptAt: nextAttemptAt ?? this.nextAttemptAt,
    attemptCount: attemptCount ?? this.attemptCount,
    lastError: lastError.present ? lastError.value : this.lastError,
  );
  ChatSyncJob copyWithCompanion(ChatSyncJobsCompanion data) {
    return ChatSyncJob(
      id: data.id.present ? data.id.value : this.id,
      userId: data.userId.present ? data.userId.value : this.userId,
      sessionId: data.sessionId.present ? data.sessionId.value : this.sessionId,
      messageId: data.messageId.present ? data.messageId.value : this.messageId,
      jobType: data.jobType.present ? data.jobType.value : this.jobType,
      createdAt: data.createdAt.present ? data.createdAt.value : this.createdAt,
      nextAttemptAt: data.nextAttemptAt.present
          ? data.nextAttemptAt.value
          : this.nextAttemptAt,
      attemptCount: data.attemptCount.present
          ? data.attemptCount.value
          : this.attemptCount,
      lastError: data.lastError.present ? data.lastError.value : this.lastError,
    );
  }

  @override
  String toString() {
    return (StringBuffer('ChatSyncJob(')
          ..write('id: $id, ')
          ..write('userId: $userId, ')
          ..write('sessionId: $sessionId, ')
          ..write('messageId: $messageId, ')
          ..write('jobType: $jobType, ')
          ..write('createdAt: $createdAt, ')
          ..write('nextAttemptAt: $nextAttemptAt, ')
          ..write('attemptCount: $attemptCount, ')
          ..write('lastError: $lastError')
          ..write(')'))
        .toString();
  }

  @override
  int get hashCode => Object.hash(
    id,
    userId,
    sessionId,
    messageId,
    jobType,
    createdAt,
    nextAttemptAt,
    attemptCount,
    lastError,
  );
  @override
  bool operator ==(Object other) =>
      identical(this, other) ||
      (other is ChatSyncJob &&
          other.id == this.id &&
          other.userId == this.userId &&
          other.sessionId == this.sessionId &&
          other.messageId == this.messageId &&
          other.jobType == this.jobType &&
          other.createdAt == this.createdAt &&
          other.nextAttemptAt == this.nextAttemptAt &&
          other.attemptCount == this.attemptCount &&
          other.lastError == this.lastError);
}

class ChatSyncJobsCompanion extends UpdateCompanion<ChatSyncJob> {
  final Value<int> id;
  final Value<String> userId;
  final Value<String> sessionId;
  final Value<String?> messageId;
  final Value<String> jobType;
  final Value<DateTime> createdAt;
  final Value<DateTime> nextAttemptAt;
  final Value<int> attemptCount;
  final Value<String?> lastError;
  const ChatSyncJobsCompanion({
    this.id = const Value.absent(),
    this.userId = const Value.absent(),
    this.sessionId = const Value.absent(),
    this.messageId = const Value.absent(),
    this.jobType = const Value.absent(),
    this.createdAt = const Value.absent(),
    this.nextAttemptAt = const Value.absent(),
    this.attemptCount = const Value.absent(),
    this.lastError = const Value.absent(),
  });
  ChatSyncJobsCompanion.insert({
    this.id = const Value.absent(),
    required String userId,
    required String sessionId,
    this.messageId = const Value.absent(),
    required String jobType,
    this.createdAt = const Value.absent(),
    this.nextAttemptAt = const Value.absent(),
    this.attemptCount = const Value.absent(),
    this.lastError = const Value.absent(),
  }) : userId = Value(userId),
       sessionId = Value(sessionId),
       jobType = Value(jobType);
  static Insertable<ChatSyncJob> custom({
    Expression<int>? id,
    Expression<String>? userId,
    Expression<String>? sessionId,
    Expression<String>? messageId,
    Expression<String>? jobType,
    Expression<DateTime>? createdAt,
    Expression<DateTime>? nextAttemptAt,
    Expression<int>? attemptCount,
    Expression<String>? lastError,
  }) {
    return RawValuesInsertable({
      if (id != null) 'id': id,
      if (userId != null) 'user_id': userId,
      if (sessionId != null) 'session_id': sessionId,
      if (messageId != null) 'message_id': messageId,
      if (jobType != null) 'job_type': jobType,
      if (createdAt != null) 'created_at': createdAt,
      if (nextAttemptAt != null) 'next_attempt_at': nextAttemptAt,
      if (attemptCount != null) 'attempt_count': attemptCount,
      if (lastError != null) 'last_error': lastError,
    });
  }

  ChatSyncJobsCompanion copyWith({
    Value<int>? id,
    Value<String>? userId,
    Value<String>? sessionId,
    Value<String?>? messageId,
    Value<String>? jobType,
    Value<DateTime>? createdAt,
    Value<DateTime>? nextAttemptAt,
    Value<int>? attemptCount,
    Value<String?>? lastError,
  }) {
    return ChatSyncJobsCompanion(
      id: id ?? this.id,
      userId: userId ?? this.userId,
      sessionId: sessionId ?? this.sessionId,
      messageId: messageId ?? this.messageId,
      jobType: jobType ?? this.jobType,
      createdAt: createdAt ?? this.createdAt,
      nextAttemptAt: nextAttemptAt ?? this.nextAttemptAt,
      attemptCount: attemptCount ?? this.attemptCount,
      lastError: lastError ?? this.lastError,
    );
  }

  @override
  Map<String, Expression> toColumns(bool nullToAbsent) {
    final map = <String, Expression>{};
    if (id.present) {
      map['id'] = Variable<int>(id.value);
    }
    if (userId.present) {
      map['user_id'] = Variable<String>(userId.value);
    }
    if (sessionId.present) {
      map['session_id'] = Variable<String>(sessionId.value);
    }
    if (messageId.present) {
      map['message_id'] = Variable<String>(messageId.value);
    }
    if (jobType.present) {
      map['job_type'] = Variable<String>(jobType.value);
    }
    if (createdAt.present) {
      map['created_at'] = Variable<DateTime>(createdAt.value);
    }
    if (nextAttemptAt.present) {
      map['next_attempt_at'] = Variable<DateTime>(nextAttemptAt.value);
    }
    if (attemptCount.present) {
      map['attempt_count'] = Variable<int>(attemptCount.value);
    }
    if (lastError.present) {
      map['last_error'] = Variable<String>(lastError.value);
    }
    return map;
  }

  @override
  String toString() {
    return (StringBuffer('ChatSyncJobsCompanion(')
          ..write('id: $id, ')
          ..write('userId: $userId, ')
          ..write('sessionId: $sessionId, ')
          ..write('messageId: $messageId, ')
          ..write('jobType: $jobType, ')
          ..write('createdAt: $createdAt, ')
          ..write('nextAttemptAt: $nextAttemptAt, ')
          ..write('attemptCount: $attemptCount, ')
          ..write('lastError: $lastError')
          ..write(')'))
        .toString();
  }
}

class $GetBetterCatalogCachesTable extends GetBetterCatalogCaches
    with TableInfo<$GetBetterCatalogCachesTable, GetBetterCatalogCache> {
  @override
  final GeneratedDatabase attachedDatabase;
  final String? _alias;
  $GetBetterCatalogCachesTable(this.attachedDatabase, [this._alias]);
  static const VerificationMeta _cacheKeyMeta = const VerificationMeta(
    'cacheKey',
  );
  @override
  late final GeneratedColumn<String> cacheKey = GeneratedColumn<String>(
    'cache_key',
    aliasedName,
    false,
    type: DriftSqlType.string,
    requiredDuringInsert: true,
  );
  static const VerificationMeta _catalogVersionMeta = const VerificationMeta(
    'catalogVersion',
  );
  @override
  late final GeneratedColumn<String> catalogVersion = GeneratedColumn<String>(
    'catalog_version',
    aliasedName,
    false,
    type: DriftSqlType.string,
    requiredDuringInsert: true,
  );
  static const VerificationMeta _feedJsonMeta = const VerificationMeta(
    'feedJson',
  );
  @override
  late final GeneratedColumn<String> feedJson = GeneratedColumn<String>(
    'feed_json',
    aliasedName,
    false,
    type: DriftSqlType.string,
    requiredDuringInsert: true,
  );
  static const VerificationMeta _checkedAtMeta = const VerificationMeta(
    'checkedAt',
  );
  @override
  late final GeneratedColumn<DateTime> checkedAt = GeneratedColumn<DateTime>(
    'checked_at',
    aliasedName,
    false,
    type: DriftSqlType.dateTime,
    requiredDuringInsert: true,
  );
  static const VerificationMeta _updatedAtMeta = const VerificationMeta(
    'updatedAt',
  );
  @override
  late final GeneratedColumn<DateTime> updatedAt = GeneratedColumn<DateTime>(
    'updated_at',
    aliasedName,
    false,
    type: DriftSqlType.dateTime,
    requiredDuringInsert: true,
  );
  @override
  List<GeneratedColumn> get $columns => [
    cacheKey,
    catalogVersion,
    feedJson,
    checkedAt,
    updatedAt,
  ];
  @override
  String get aliasedName => _alias ?? actualTableName;
  @override
  String get actualTableName => $name;
  static const String $name = 'get_better_catalog_caches';
  @override
  VerificationContext validateIntegrity(
    Insertable<GetBetterCatalogCache> instance, {
    bool isInserting = false,
  }) {
    final context = VerificationContext();
    final data = instance.toColumns(true);
    if (data.containsKey('cache_key')) {
      context.handle(
        _cacheKeyMeta,
        cacheKey.isAcceptableOrUnknown(data['cache_key']!, _cacheKeyMeta),
      );
    } else if (isInserting) {
      context.missing(_cacheKeyMeta);
    }
    if (data.containsKey('catalog_version')) {
      context.handle(
        _catalogVersionMeta,
        catalogVersion.isAcceptableOrUnknown(
          data['catalog_version']!,
          _catalogVersionMeta,
        ),
      );
    } else if (isInserting) {
      context.missing(_catalogVersionMeta);
    }
    if (data.containsKey('feed_json')) {
      context.handle(
        _feedJsonMeta,
        feedJson.isAcceptableOrUnknown(data['feed_json']!, _feedJsonMeta),
      );
    } else if (isInserting) {
      context.missing(_feedJsonMeta);
    }
    if (data.containsKey('checked_at')) {
      context.handle(
        _checkedAtMeta,
        checkedAt.isAcceptableOrUnknown(data['checked_at']!, _checkedAtMeta),
      );
    } else if (isInserting) {
      context.missing(_checkedAtMeta);
    }
    if (data.containsKey('updated_at')) {
      context.handle(
        _updatedAtMeta,
        updatedAt.isAcceptableOrUnknown(data['updated_at']!, _updatedAtMeta),
      );
    } else if (isInserting) {
      context.missing(_updatedAtMeta);
    }
    return context;
  }

  @override
  Set<GeneratedColumn> get $primaryKey => {cacheKey};
  @override
  GetBetterCatalogCache map(Map<String, dynamic> data, {String? tablePrefix}) {
    final effectivePrefix = tablePrefix != null ? '$tablePrefix.' : '';
    return GetBetterCatalogCache(
      cacheKey: attachedDatabase.typeMapping.read(
        DriftSqlType.string,
        data['${effectivePrefix}cache_key'],
      )!,
      catalogVersion: attachedDatabase.typeMapping.read(
        DriftSqlType.string,
        data['${effectivePrefix}catalog_version'],
      )!,
      feedJson: attachedDatabase.typeMapping.read(
        DriftSqlType.string,
        data['${effectivePrefix}feed_json'],
      )!,
      checkedAt: attachedDatabase.typeMapping.read(
        DriftSqlType.dateTime,
        data['${effectivePrefix}checked_at'],
      )!,
      updatedAt: attachedDatabase.typeMapping.read(
        DriftSqlType.dateTime,
        data['${effectivePrefix}updated_at'],
      )!,
    );
  }

  @override
  $GetBetterCatalogCachesTable createAlias(String alias) {
    return $GetBetterCatalogCachesTable(attachedDatabase, alias);
  }
}

class GetBetterCatalogCache extends DataClass
    implements Insertable<GetBetterCatalogCache> {
  final String cacheKey;
  final String catalogVersion;
  final String feedJson;
  final DateTime checkedAt;
  final DateTime updatedAt;
  const GetBetterCatalogCache({
    required this.cacheKey,
    required this.catalogVersion,
    required this.feedJson,
    required this.checkedAt,
    required this.updatedAt,
  });
  @override
  Map<String, Expression> toColumns(bool nullToAbsent) {
    final map = <String, Expression>{};
    map['cache_key'] = Variable<String>(cacheKey);
    map['catalog_version'] = Variable<String>(catalogVersion);
    map['feed_json'] = Variable<String>(feedJson);
    map['checked_at'] = Variable<DateTime>(checkedAt);
    map['updated_at'] = Variable<DateTime>(updatedAt);
    return map;
  }

  GetBetterCatalogCachesCompanion toCompanion(bool nullToAbsent) {
    return GetBetterCatalogCachesCompanion(
      cacheKey: Value(cacheKey),
      catalogVersion: Value(catalogVersion),
      feedJson: Value(feedJson),
      checkedAt: Value(checkedAt),
      updatedAt: Value(updatedAt),
    );
  }

  factory GetBetterCatalogCache.fromJson(
    Map<String, dynamic> json, {
    ValueSerializer? serializer,
  }) {
    serializer ??= driftRuntimeOptions.defaultSerializer;
    return GetBetterCatalogCache(
      cacheKey: serializer.fromJson<String>(json['cacheKey']),
      catalogVersion: serializer.fromJson<String>(json['catalogVersion']),
      feedJson: serializer.fromJson<String>(json['feedJson']),
      checkedAt: serializer.fromJson<DateTime>(json['checkedAt']),
      updatedAt: serializer.fromJson<DateTime>(json['updatedAt']),
    );
  }
  @override
  Map<String, dynamic> toJson({ValueSerializer? serializer}) {
    serializer ??= driftRuntimeOptions.defaultSerializer;
    return <String, dynamic>{
      'cacheKey': serializer.toJson<String>(cacheKey),
      'catalogVersion': serializer.toJson<String>(catalogVersion),
      'feedJson': serializer.toJson<String>(feedJson),
      'checkedAt': serializer.toJson<DateTime>(checkedAt),
      'updatedAt': serializer.toJson<DateTime>(updatedAt),
    };
  }

  GetBetterCatalogCache copyWith({
    String? cacheKey,
    String? catalogVersion,
    String? feedJson,
    DateTime? checkedAt,
    DateTime? updatedAt,
  }) => GetBetterCatalogCache(
    cacheKey: cacheKey ?? this.cacheKey,
    catalogVersion: catalogVersion ?? this.catalogVersion,
    feedJson: feedJson ?? this.feedJson,
    checkedAt: checkedAt ?? this.checkedAt,
    updatedAt: updatedAt ?? this.updatedAt,
  );
  GetBetterCatalogCache copyWithCompanion(
    GetBetterCatalogCachesCompanion data,
  ) {
    return GetBetterCatalogCache(
      cacheKey: data.cacheKey.present ? data.cacheKey.value : this.cacheKey,
      catalogVersion: data.catalogVersion.present
          ? data.catalogVersion.value
          : this.catalogVersion,
      feedJson: data.feedJson.present ? data.feedJson.value : this.feedJson,
      checkedAt: data.checkedAt.present ? data.checkedAt.value : this.checkedAt,
      updatedAt: data.updatedAt.present ? data.updatedAt.value : this.updatedAt,
    );
  }

  @override
  String toString() {
    return (StringBuffer('GetBetterCatalogCache(')
          ..write('cacheKey: $cacheKey, ')
          ..write('catalogVersion: $catalogVersion, ')
          ..write('feedJson: $feedJson, ')
          ..write('checkedAt: $checkedAt, ')
          ..write('updatedAt: $updatedAt')
          ..write(')'))
        .toString();
  }

  @override
  int get hashCode =>
      Object.hash(cacheKey, catalogVersion, feedJson, checkedAt, updatedAt);
  @override
  bool operator ==(Object other) =>
      identical(this, other) ||
      (other is GetBetterCatalogCache &&
          other.cacheKey == this.cacheKey &&
          other.catalogVersion == this.catalogVersion &&
          other.feedJson == this.feedJson &&
          other.checkedAt == this.checkedAt &&
          other.updatedAt == this.updatedAt);
}

class GetBetterCatalogCachesCompanion
    extends UpdateCompanion<GetBetterCatalogCache> {
  final Value<String> cacheKey;
  final Value<String> catalogVersion;
  final Value<String> feedJson;
  final Value<DateTime> checkedAt;
  final Value<DateTime> updatedAt;
  final Value<int> rowid;
  const GetBetterCatalogCachesCompanion({
    this.cacheKey = const Value.absent(),
    this.catalogVersion = const Value.absent(),
    this.feedJson = const Value.absent(),
    this.checkedAt = const Value.absent(),
    this.updatedAt = const Value.absent(),
    this.rowid = const Value.absent(),
  });
  GetBetterCatalogCachesCompanion.insert({
    required String cacheKey,
    required String catalogVersion,
    required String feedJson,
    required DateTime checkedAt,
    required DateTime updatedAt,
    this.rowid = const Value.absent(),
  }) : cacheKey = Value(cacheKey),
       catalogVersion = Value(catalogVersion),
       feedJson = Value(feedJson),
       checkedAt = Value(checkedAt),
       updatedAt = Value(updatedAt);
  static Insertable<GetBetterCatalogCache> custom({
    Expression<String>? cacheKey,
    Expression<String>? catalogVersion,
    Expression<String>? feedJson,
    Expression<DateTime>? checkedAt,
    Expression<DateTime>? updatedAt,
    Expression<int>? rowid,
  }) {
    return RawValuesInsertable({
      if (cacheKey != null) 'cache_key': cacheKey,
      if (catalogVersion != null) 'catalog_version': catalogVersion,
      if (feedJson != null) 'feed_json': feedJson,
      if (checkedAt != null) 'checked_at': checkedAt,
      if (updatedAt != null) 'updated_at': updatedAt,
      if (rowid != null) 'rowid': rowid,
    });
  }

  GetBetterCatalogCachesCompanion copyWith({
    Value<String>? cacheKey,
    Value<String>? catalogVersion,
    Value<String>? feedJson,
    Value<DateTime>? checkedAt,
    Value<DateTime>? updatedAt,
    Value<int>? rowid,
  }) {
    return GetBetterCatalogCachesCompanion(
      cacheKey: cacheKey ?? this.cacheKey,
      catalogVersion: catalogVersion ?? this.catalogVersion,
      feedJson: feedJson ?? this.feedJson,
      checkedAt: checkedAt ?? this.checkedAt,
      updatedAt: updatedAt ?? this.updatedAt,
      rowid: rowid ?? this.rowid,
    );
  }

  @override
  Map<String, Expression> toColumns(bool nullToAbsent) {
    final map = <String, Expression>{};
    if (cacheKey.present) {
      map['cache_key'] = Variable<String>(cacheKey.value);
    }
    if (catalogVersion.present) {
      map['catalog_version'] = Variable<String>(catalogVersion.value);
    }
    if (feedJson.present) {
      map['feed_json'] = Variable<String>(feedJson.value);
    }
    if (checkedAt.present) {
      map['checked_at'] = Variable<DateTime>(checkedAt.value);
    }
    if (updatedAt.present) {
      map['updated_at'] = Variable<DateTime>(updatedAt.value);
    }
    if (rowid.present) {
      map['rowid'] = Variable<int>(rowid.value);
    }
    return map;
  }

  @override
  String toString() {
    return (StringBuffer('GetBetterCatalogCachesCompanion(')
          ..write('cacheKey: $cacheKey, ')
          ..write('catalogVersion: $catalogVersion, ')
          ..write('feedJson: $feedJson, ')
          ..write('checkedAt: $checkedAt, ')
          ..write('updatedAt: $updatedAt, ')
          ..write('rowid: $rowid')
          ..write(')'))
        .toString();
  }
}

class $GetBetterStoryProgressTable extends GetBetterStoryProgress
    with TableInfo<$GetBetterStoryProgressTable, GetBetterStoryProgressData> {
  @override
  final GeneratedDatabase attachedDatabase;
  final String? _alias;
  $GetBetterStoryProgressTable(this.attachedDatabase, [this._alias]);
  static const VerificationMeta _userIdMeta = const VerificationMeta('userId');
  @override
  late final GeneratedColumn<String> userId = GeneratedColumn<String>(
    'user_id',
    aliasedName,
    false,
    type: DriftSqlType.string,
    requiredDuringInsert: true,
  );
  static const VerificationMeta _storyIdMeta = const VerificationMeta(
    'storyId',
  );
  @override
  late final GeneratedColumn<String> storyId = GeneratedColumn<String>(
    'story_id',
    aliasedName,
    false,
    type: DriftSqlType.string,
    requiredDuringInsert: true,
  );
  static const VerificationMeta _storyVersionMeta = const VerificationMeta(
    'storyVersion',
  );
  @override
  late final GeneratedColumn<int> storyVersion = GeneratedColumn<int>(
    'story_version',
    aliasedName,
    false,
    type: DriftSqlType.int,
    requiredDuringInsert: false,
    defaultValue: const Constant(1),
  );
  static const VerificationMeta _openedMeta = const VerificationMeta('opened');
  @override
  late final GeneratedColumn<bool> opened = GeneratedColumn<bool>(
    'opened',
    aliasedName,
    false,
    type: DriftSqlType.bool,
    requiredDuringInsert: false,
    defaultConstraints: GeneratedColumn.constraintIsAlways(
      'CHECK ("opened" IN (0, 1))',
    ),
    defaultValue: const Constant(false),
  );
  static const VerificationMeta _savedMeta = const VerificationMeta('saved');
  @override
  late final GeneratedColumn<bool> saved = GeneratedColumn<bool>(
    'saved',
    aliasedName,
    false,
    type: DriftSqlType.bool,
    requiredDuringInsert: false,
    defaultConstraints: GeneratedColumn.constraintIsAlways(
      'CHECK ("saved" IN (0, 1))',
    ),
    defaultValue: const Constant(false),
  );
  static const VerificationMeta _completedMeta = const VerificationMeta(
    'completed',
  );
  @override
  late final GeneratedColumn<bool> completed = GeneratedColumn<bool>(
    'completed',
    aliasedName,
    false,
    type: DriftSqlType.bool,
    requiredDuringInsert: false,
    defaultConstraints: GeneratedColumn.constraintIsAlways(
      'CHECK ("completed" IN (0, 1))',
    ),
    defaultValue: const Constant(false),
  );
  static const VerificationMeta _lastOpenedAtMeta = const VerificationMeta(
    'lastOpenedAt',
  );
  @override
  late final GeneratedColumn<DateTime> lastOpenedAt = GeneratedColumn<DateTime>(
    'last_opened_at',
    aliasedName,
    true,
    type: DriftSqlType.dateTime,
    requiredDuringInsert: false,
  );
  static const VerificationMeta _updatedAtMeta = const VerificationMeta(
    'updatedAt',
  );
  @override
  late final GeneratedColumn<DateTime> updatedAt = GeneratedColumn<DateTime>(
    'updated_at',
    aliasedName,
    false,
    type: DriftSqlType.dateTime,
    requiredDuringInsert: false,
    defaultValue: currentDateAndTime,
  );
  @override
  List<GeneratedColumn> get $columns => [
    userId,
    storyId,
    storyVersion,
    opened,
    saved,
    completed,
    lastOpenedAt,
    updatedAt,
  ];
  @override
  String get aliasedName => _alias ?? actualTableName;
  @override
  String get actualTableName => $name;
  static const String $name = 'get_better_story_progress';
  @override
  VerificationContext validateIntegrity(
    Insertable<GetBetterStoryProgressData> instance, {
    bool isInserting = false,
  }) {
    final context = VerificationContext();
    final data = instance.toColumns(true);
    if (data.containsKey('user_id')) {
      context.handle(
        _userIdMeta,
        userId.isAcceptableOrUnknown(data['user_id']!, _userIdMeta),
      );
    } else if (isInserting) {
      context.missing(_userIdMeta);
    }
    if (data.containsKey('story_id')) {
      context.handle(
        _storyIdMeta,
        storyId.isAcceptableOrUnknown(data['story_id']!, _storyIdMeta),
      );
    } else if (isInserting) {
      context.missing(_storyIdMeta);
    }
    if (data.containsKey('story_version')) {
      context.handle(
        _storyVersionMeta,
        storyVersion.isAcceptableOrUnknown(
          data['story_version']!,
          _storyVersionMeta,
        ),
      );
    }
    if (data.containsKey('opened')) {
      context.handle(
        _openedMeta,
        opened.isAcceptableOrUnknown(data['opened']!, _openedMeta),
      );
    }
    if (data.containsKey('saved')) {
      context.handle(
        _savedMeta,
        saved.isAcceptableOrUnknown(data['saved']!, _savedMeta),
      );
    }
    if (data.containsKey('completed')) {
      context.handle(
        _completedMeta,
        completed.isAcceptableOrUnknown(data['completed']!, _completedMeta),
      );
    }
    if (data.containsKey('last_opened_at')) {
      context.handle(
        _lastOpenedAtMeta,
        lastOpenedAt.isAcceptableOrUnknown(
          data['last_opened_at']!,
          _lastOpenedAtMeta,
        ),
      );
    }
    if (data.containsKey('updated_at')) {
      context.handle(
        _updatedAtMeta,
        updatedAt.isAcceptableOrUnknown(data['updated_at']!, _updatedAtMeta),
      );
    }
    return context;
  }

  @override
  Set<GeneratedColumn> get $primaryKey => {userId, storyId};
  @override
  GetBetterStoryProgressData map(
    Map<String, dynamic> data, {
    String? tablePrefix,
  }) {
    final effectivePrefix = tablePrefix != null ? '$tablePrefix.' : '';
    return GetBetterStoryProgressData(
      userId: attachedDatabase.typeMapping.read(
        DriftSqlType.string,
        data['${effectivePrefix}user_id'],
      )!,
      storyId: attachedDatabase.typeMapping.read(
        DriftSqlType.string,
        data['${effectivePrefix}story_id'],
      )!,
      storyVersion: attachedDatabase.typeMapping.read(
        DriftSqlType.int,
        data['${effectivePrefix}story_version'],
      )!,
      opened: attachedDatabase.typeMapping.read(
        DriftSqlType.bool,
        data['${effectivePrefix}opened'],
      )!,
      saved: attachedDatabase.typeMapping.read(
        DriftSqlType.bool,
        data['${effectivePrefix}saved'],
      )!,
      completed: attachedDatabase.typeMapping.read(
        DriftSqlType.bool,
        data['${effectivePrefix}completed'],
      )!,
      lastOpenedAt: attachedDatabase.typeMapping.read(
        DriftSqlType.dateTime,
        data['${effectivePrefix}last_opened_at'],
      ),
      updatedAt: attachedDatabase.typeMapping.read(
        DriftSqlType.dateTime,
        data['${effectivePrefix}updated_at'],
      )!,
    );
  }

  @override
  $GetBetterStoryProgressTable createAlias(String alias) {
    return $GetBetterStoryProgressTable(attachedDatabase, alias);
  }
}

class GetBetterStoryProgressData extends DataClass
    implements Insertable<GetBetterStoryProgressData> {
  final String userId;
  final String storyId;
  final int storyVersion;
  final bool opened;
  final bool saved;
  final bool completed;
  final DateTime? lastOpenedAt;
  final DateTime updatedAt;
  const GetBetterStoryProgressData({
    required this.userId,
    required this.storyId,
    required this.storyVersion,
    required this.opened,
    required this.saved,
    required this.completed,
    this.lastOpenedAt,
    required this.updatedAt,
  });
  @override
  Map<String, Expression> toColumns(bool nullToAbsent) {
    final map = <String, Expression>{};
    map['user_id'] = Variable<String>(userId);
    map['story_id'] = Variable<String>(storyId);
    map['story_version'] = Variable<int>(storyVersion);
    map['opened'] = Variable<bool>(opened);
    map['saved'] = Variable<bool>(saved);
    map['completed'] = Variable<bool>(completed);
    if (!nullToAbsent || lastOpenedAt != null) {
      map['last_opened_at'] = Variable<DateTime>(lastOpenedAt);
    }
    map['updated_at'] = Variable<DateTime>(updatedAt);
    return map;
  }

  GetBetterStoryProgressCompanion toCompanion(bool nullToAbsent) {
    return GetBetterStoryProgressCompanion(
      userId: Value(userId),
      storyId: Value(storyId),
      storyVersion: Value(storyVersion),
      opened: Value(opened),
      saved: Value(saved),
      completed: Value(completed),
      lastOpenedAt: lastOpenedAt == null && nullToAbsent
          ? const Value.absent()
          : Value(lastOpenedAt),
      updatedAt: Value(updatedAt),
    );
  }

  factory GetBetterStoryProgressData.fromJson(
    Map<String, dynamic> json, {
    ValueSerializer? serializer,
  }) {
    serializer ??= driftRuntimeOptions.defaultSerializer;
    return GetBetterStoryProgressData(
      userId: serializer.fromJson<String>(json['userId']),
      storyId: serializer.fromJson<String>(json['storyId']),
      storyVersion: serializer.fromJson<int>(json['storyVersion']),
      opened: serializer.fromJson<bool>(json['opened']),
      saved: serializer.fromJson<bool>(json['saved']),
      completed: serializer.fromJson<bool>(json['completed']),
      lastOpenedAt: serializer.fromJson<DateTime?>(json['lastOpenedAt']),
      updatedAt: serializer.fromJson<DateTime>(json['updatedAt']),
    );
  }
  @override
  Map<String, dynamic> toJson({ValueSerializer? serializer}) {
    serializer ??= driftRuntimeOptions.defaultSerializer;
    return <String, dynamic>{
      'userId': serializer.toJson<String>(userId),
      'storyId': serializer.toJson<String>(storyId),
      'storyVersion': serializer.toJson<int>(storyVersion),
      'opened': serializer.toJson<bool>(opened),
      'saved': serializer.toJson<bool>(saved),
      'completed': serializer.toJson<bool>(completed),
      'lastOpenedAt': serializer.toJson<DateTime?>(lastOpenedAt),
      'updatedAt': serializer.toJson<DateTime>(updatedAt),
    };
  }

  GetBetterStoryProgressData copyWith({
    String? userId,
    String? storyId,
    int? storyVersion,
    bool? opened,
    bool? saved,
    bool? completed,
    Value<DateTime?> lastOpenedAt = const Value.absent(),
    DateTime? updatedAt,
  }) => GetBetterStoryProgressData(
    userId: userId ?? this.userId,
    storyId: storyId ?? this.storyId,
    storyVersion: storyVersion ?? this.storyVersion,
    opened: opened ?? this.opened,
    saved: saved ?? this.saved,
    completed: completed ?? this.completed,
    lastOpenedAt: lastOpenedAt.present ? lastOpenedAt.value : this.lastOpenedAt,
    updatedAt: updatedAt ?? this.updatedAt,
  );
  GetBetterStoryProgressData copyWithCompanion(
    GetBetterStoryProgressCompanion data,
  ) {
    return GetBetterStoryProgressData(
      userId: data.userId.present ? data.userId.value : this.userId,
      storyId: data.storyId.present ? data.storyId.value : this.storyId,
      storyVersion: data.storyVersion.present
          ? data.storyVersion.value
          : this.storyVersion,
      opened: data.opened.present ? data.opened.value : this.opened,
      saved: data.saved.present ? data.saved.value : this.saved,
      completed: data.completed.present ? data.completed.value : this.completed,
      lastOpenedAt: data.lastOpenedAt.present
          ? data.lastOpenedAt.value
          : this.lastOpenedAt,
      updatedAt: data.updatedAt.present ? data.updatedAt.value : this.updatedAt,
    );
  }

  @override
  String toString() {
    return (StringBuffer('GetBetterStoryProgressData(')
          ..write('userId: $userId, ')
          ..write('storyId: $storyId, ')
          ..write('storyVersion: $storyVersion, ')
          ..write('opened: $opened, ')
          ..write('saved: $saved, ')
          ..write('completed: $completed, ')
          ..write('lastOpenedAt: $lastOpenedAt, ')
          ..write('updatedAt: $updatedAt')
          ..write(')'))
        .toString();
  }

  @override
  int get hashCode => Object.hash(
    userId,
    storyId,
    storyVersion,
    opened,
    saved,
    completed,
    lastOpenedAt,
    updatedAt,
  );
  @override
  bool operator ==(Object other) =>
      identical(this, other) ||
      (other is GetBetterStoryProgressData &&
          other.userId == this.userId &&
          other.storyId == this.storyId &&
          other.storyVersion == this.storyVersion &&
          other.opened == this.opened &&
          other.saved == this.saved &&
          other.completed == this.completed &&
          other.lastOpenedAt == this.lastOpenedAt &&
          other.updatedAt == this.updatedAt);
}

class GetBetterStoryProgressCompanion
    extends UpdateCompanion<GetBetterStoryProgressData> {
  final Value<String> userId;
  final Value<String> storyId;
  final Value<int> storyVersion;
  final Value<bool> opened;
  final Value<bool> saved;
  final Value<bool> completed;
  final Value<DateTime?> lastOpenedAt;
  final Value<DateTime> updatedAt;
  final Value<int> rowid;
  const GetBetterStoryProgressCompanion({
    this.userId = const Value.absent(),
    this.storyId = const Value.absent(),
    this.storyVersion = const Value.absent(),
    this.opened = const Value.absent(),
    this.saved = const Value.absent(),
    this.completed = const Value.absent(),
    this.lastOpenedAt = const Value.absent(),
    this.updatedAt = const Value.absent(),
    this.rowid = const Value.absent(),
  });
  GetBetterStoryProgressCompanion.insert({
    required String userId,
    required String storyId,
    this.storyVersion = const Value.absent(),
    this.opened = const Value.absent(),
    this.saved = const Value.absent(),
    this.completed = const Value.absent(),
    this.lastOpenedAt = const Value.absent(),
    this.updatedAt = const Value.absent(),
    this.rowid = const Value.absent(),
  }) : userId = Value(userId),
       storyId = Value(storyId);
  static Insertable<GetBetterStoryProgressData> custom({
    Expression<String>? userId,
    Expression<String>? storyId,
    Expression<int>? storyVersion,
    Expression<bool>? opened,
    Expression<bool>? saved,
    Expression<bool>? completed,
    Expression<DateTime>? lastOpenedAt,
    Expression<DateTime>? updatedAt,
    Expression<int>? rowid,
  }) {
    return RawValuesInsertable({
      if (userId != null) 'user_id': userId,
      if (storyId != null) 'story_id': storyId,
      if (storyVersion != null) 'story_version': storyVersion,
      if (opened != null) 'opened': opened,
      if (saved != null) 'saved': saved,
      if (completed != null) 'completed': completed,
      if (lastOpenedAt != null) 'last_opened_at': lastOpenedAt,
      if (updatedAt != null) 'updated_at': updatedAt,
      if (rowid != null) 'rowid': rowid,
    });
  }

  GetBetterStoryProgressCompanion copyWith({
    Value<String>? userId,
    Value<String>? storyId,
    Value<int>? storyVersion,
    Value<bool>? opened,
    Value<bool>? saved,
    Value<bool>? completed,
    Value<DateTime?>? lastOpenedAt,
    Value<DateTime>? updatedAt,
    Value<int>? rowid,
  }) {
    return GetBetterStoryProgressCompanion(
      userId: userId ?? this.userId,
      storyId: storyId ?? this.storyId,
      storyVersion: storyVersion ?? this.storyVersion,
      opened: opened ?? this.opened,
      saved: saved ?? this.saved,
      completed: completed ?? this.completed,
      lastOpenedAt: lastOpenedAt ?? this.lastOpenedAt,
      updatedAt: updatedAt ?? this.updatedAt,
      rowid: rowid ?? this.rowid,
    );
  }

  @override
  Map<String, Expression> toColumns(bool nullToAbsent) {
    final map = <String, Expression>{};
    if (userId.present) {
      map['user_id'] = Variable<String>(userId.value);
    }
    if (storyId.present) {
      map['story_id'] = Variable<String>(storyId.value);
    }
    if (storyVersion.present) {
      map['story_version'] = Variable<int>(storyVersion.value);
    }
    if (opened.present) {
      map['opened'] = Variable<bool>(opened.value);
    }
    if (saved.present) {
      map['saved'] = Variable<bool>(saved.value);
    }
    if (completed.present) {
      map['completed'] = Variable<bool>(completed.value);
    }
    if (lastOpenedAt.present) {
      map['last_opened_at'] = Variable<DateTime>(lastOpenedAt.value);
    }
    if (updatedAt.present) {
      map['updated_at'] = Variable<DateTime>(updatedAt.value);
    }
    if (rowid.present) {
      map['rowid'] = Variable<int>(rowid.value);
    }
    return map;
  }

  @override
  String toString() {
    return (StringBuffer('GetBetterStoryProgressCompanion(')
          ..write('userId: $userId, ')
          ..write('storyId: $storyId, ')
          ..write('storyVersion: $storyVersion, ')
          ..write('opened: $opened, ')
          ..write('saved: $saved, ')
          ..write('completed: $completed, ')
          ..write('lastOpenedAt: $lastOpenedAt, ')
          ..write('updatedAt: $updatedAt, ')
          ..write('rowid: $rowid')
          ..write(')'))
        .toString();
  }
}

class $GetBetterEventOutboxTable extends GetBetterEventOutbox
    with TableInfo<$GetBetterEventOutboxTable, GetBetterEventOutboxData> {
  @override
  final GeneratedDatabase attachedDatabase;
  final String? _alias;
  $GetBetterEventOutboxTable(this.attachedDatabase, [this._alias]);
  static const VerificationMeta _eventIdMeta = const VerificationMeta(
    'eventId',
  );
  @override
  late final GeneratedColumn<String> eventId = GeneratedColumn<String>(
    'event_id',
    aliasedName,
    false,
    type: DriftSqlType.string,
    requiredDuringInsert: true,
  );
  static const VerificationMeta _userIdMeta = const VerificationMeta('userId');
  @override
  late final GeneratedColumn<String> userId = GeneratedColumn<String>(
    'user_id',
    aliasedName,
    false,
    type: DriftSqlType.string,
    requiredDuringInsert: true,
  );
  static const VerificationMeta _eventTypeMeta = const VerificationMeta(
    'eventType',
  );
  @override
  late final GeneratedColumn<String> eventType = GeneratedColumn<String>(
    'event_type',
    aliasedName,
    false,
    type: DriftSqlType.string,
    requiredDuringInsert: true,
  );
  static const VerificationMeta _storyIdMeta = const VerificationMeta(
    'storyId',
  );
  @override
  late final GeneratedColumn<String> storyId = GeneratedColumn<String>(
    'story_id',
    aliasedName,
    false,
    type: DriftSqlType.string,
    requiredDuringInsert: true,
  );
  static const VerificationMeta _storyVersionMeta = const VerificationMeta(
    'storyVersion',
  );
  @override
  late final GeneratedColumn<int> storyVersion = GeneratedColumn<int>(
    'story_version',
    aliasedName,
    false,
    type: DriftSqlType.int,
    requiredDuringInsert: true,
  );
  static const VerificationMeta _occurredAtMeta = const VerificationMeta(
    'occurredAt',
  );
  @override
  late final GeneratedColumn<DateTime> occurredAt = GeneratedColumn<DateTime>(
    'occurred_at',
    aliasedName,
    false,
    type: DriftSqlType.dateTime,
    requiredDuringInsert: true,
  );
  @override
  List<GeneratedColumn> get $columns => [
    eventId,
    userId,
    eventType,
    storyId,
    storyVersion,
    occurredAt,
  ];
  @override
  String get aliasedName => _alias ?? actualTableName;
  @override
  String get actualTableName => $name;
  static const String $name = 'get_better_event_outbox';
  @override
  VerificationContext validateIntegrity(
    Insertable<GetBetterEventOutboxData> instance, {
    bool isInserting = false,
  }) {
    final context = VerificationContext();
    final data = instance.toColumns(true);
    if (data.containsKey('event_id')) {
      context.handle(
        _eventIdMeta,
        eventId.isAcceptableOrUnknown(data['event_id']!, _eventIdMeta),
      );
    } else if (isInserting) {
      context.missing(_eventIdMeta);
    }
    if (data.containsKey('user_id')) {
      context.handle(
        _userIdMeta,
        userId.isAcceptableOrUnknown(data['user_id']!, _userIdMeta),
      );
    } else if (isInserting) {
      context.missing(_userIdMeta);
    }
    if (data.containsKey('event_type')) {
      context.handle(
        _eventTypeMeta,
        eventType.isAcceptableOrUnknown(data['event_type']!, _eventTypeMeta),
      );
    } else if (isInserting) {
      context.missing(_eventTypeMeta);
    }
    if (data.containsKey('story_id')) {
      context.handle(
        _storyIdMeta,
        storyId.isAcceptableOrUnknown(data['story_id']!, _storyIdMeta),
      );
    } else if (isInserting) {
      context.missing(_storyIdMeta);
    }
    if (data.containsKey('story_version')) {
      context.handle(
        _storyVersionMeta,
        storyVersion.isAcceptableOrUnknown(
          data['story_version']!,
          _storyVersionMeta,
        ),
      );
    } else if (isInserting) {
      context.missing(_storyVersionMeta);
    }
    if (data.containsKey('occurred_at')) {
      context.handle(
        _occurredAtMeta,
        occurredAt.isAcceptableOrUnknown(data['occurred_at']!, _occurredAtMeta),
      );
    } else if (isInserting) {
      context.missing(_occurredAtMeta);
    }
    return context;
  }

  @override
  Set<GeneratedColumn> get $primaryKey => {eventId};
  @override
  GetBetterEventOutboxData map(
    Map<String, dynamic> data, {
    String? tablePrefix,
  }) {
    final effectivePrefix = tablePrefix != null ? '$tablePrefix.' : '';
    return GetBetterEventOutboxData(
      eventId: attachedDatabase.typeMapping.read(
        DriftSqlType.string,
        data['${effectivePrefix}event_id'],
      )!,
      userId: attachedDatabase.typeMapping.read(
        DriftSqlType.string,
        data['${effectivePrefix}user_id'],
      )!,
      eventType: attachedDatabase.typeMapping.read(
        DriftSqlType.string,
        data['${effectivePrefix}event_type'],
      )!,
      storyId: attachedDatabase.typeMapping.read(
        DriftSqlType.string,
        data['${effectivePrefix}story_id'],
      )!,
      storyVersion: attachedDatabase.typeMapping.read(
        DriftSqlType.int,
        data['${effectivePrefix}story_version'],
      )!,
      occurredAt: attachedDatabase.typeMapping.read(
        DriftSqlType.dateTime,
        data['${effectivePrefix}occurred_at'],
      )!,
    );
  }

  @override
  $GetBetterEventOutboxTable createAlias(String alias) {
    return $GetBetterEventOutboxTable(attachedDatabase, alias);
  }
}

class GetBetterEventOutboxData extends DataClass
    implements Insertable<GetBetterEventOutboxData> {
  final String eventId;
  final String userId;
  final String eventType;
  final String storyId;
  final int storyVersion;
  final DateTime occurredAt;
  const GetBetterEventOutboxData({
    required this.eventId,
    required this.userId,
    required this.eventType,
    required this.storyId,
    required this.storyVersion,
    required this.occurredAt,
  });
  @override
  Map<String, Expression> toColumns(bool nullToAbsent) {
    final map = <String, Expression>{};
    map['event_id'] = Variable<String>(eventId);
    map['user_id'] = Variable<String>(userId);
    map['event_type'] = Variable<String>(eventType);
    map['story_id'] = Variable<String>(storyId);
    map['story_version'] = Variable<int>(storyVersion);
    map['occurred_at'] = Variable<DateTime>(occurredAt);
    return map;
  }

  GetBetterEventOutboxCompanion toCompanion(bool nullToAbsent) {
    return GetBetterEventOutboxCompanion(
      eventId: Value(eventId),
      userId: Value(userId),
      eventType: Value(eventType),
      storyId: Value(storyId),
      storyVersion: Value(storyVersion),
      occurredAt: Value(occurredAt),
    );
  }

  factory GetBetterEventOutboxData.fromJson(
    Map<String, dynamic> json, {
    ValueSerializer? serializer,
  }) {
    serializer ??= driftRuntimeOptions.defaultSerializer;
    return GetBetterEventOutboxData(
      eventId: serializer.fromJson<String>(json['eventId']),
      userId: serializer.fromJson<String>(json['userId']),
      eventType: serializer.fromJson<String>(json['eventType']),
      storyId: serializer.fromJson<String>(json['storyId']),
      storyVersion: serializer.fromJson<int>(json['storyVersion']),
      occurredAt: serializer.fromJson<DateTime>(json['occurredAt']),
    );
  }
  @override
  Map<String, dynamic> toJson({ValueSerializer? serializer}) {
    serializer ??= driftRuntimeOptions.defaultSerializer;
    return <String, dynamic>{
      'eventId': serializer.toJson<String>(eventId),
      'userId': serializer.toJson<String>(userId),
      'eventType': serializer.toJson<String>(eventType),
      'storyId': serializer.toJson<String>(storyId),
      'storyVersion': serializer.toJson<int>(storyVersion),
      'occurredAt': serializer.toJson<DateTime>(occurredAt),
    };
  }

  GetBetterEventOutboxData copyWith({
    String? eventId,
    String? userId,
    String? eventType,
    String? storyId,
    int? storyVersion,
    DateTime? occurredAt,
  }) => GetBetterEventOutboxData(
    eventId: eventId ?? this.eventId,
    userId: userId ?? this.userId,
    eventType: eventType ?? this.eventType,
    storyId: storyId ?? this.storyId,
    storyVersion: storyVersion ?? this.storyVersion,
    occurredAt: occurredAt ?? this.occurredAt,
  );
  GetBetterEventOutboxData copyWithCompanion(
    GetBetterEventOutboxCompanion data,
  ) {
    return GetBetterEventOutboxData(
      eventId: data.eventId.present ? data.eventId.value : this.eventId,
      userId: data.userId.present ? data.userId.value : this.userId,
      eventType: data.eventType.present ? data.eventType.value : this.eventType,
      storyId: data.storyId.present ? data.storyId.value : this.storyId,
      storyVersion: data.storyVersion.present
          ? data.storyVersion.value
          : this.storyVersion,
      occurredAt: data.occurredAt.present
          ? data.occurredAt.value
          : this.occurredAt,
    );
  }

  @override
  String toString() {
    return (StringBuffer('GetBetterEventOutboxData(')
          ..write('eventId: $eventId, ')
          ..write('userId: $userId, ')
          ..write('eventType: $eventType, ')
          ..write('storyId: $storyId, ')
          ..write('storyVersion: $storyVersion, ')
          ..write('occurredAt: $occurredAt')
          ..write(')'))
        .toString();
  }

  @override
  int get hashCode => Object.hash(
    eventId,
    userId,
    eventType,
    storyId,
    storyVersion,
    occurredAt,
  );
  @override
  bool operator ==(Object other) =>
      identical(this, other) ||
      (other is GetBetterEventOutboxData &&
          other.eventId == this.eventId &&
          other.userId == this.userId &&
          other.eventType == this.eventType &&
          other.storyId == this.storyId &&
          other.storyVersion == this.storyVersion &&
          other.occurredAt == this.occurredAt);
}

class GetBetterEventOutboxCompanion
    extends UpdateCompanion<GetBetterEventOutboxData> {
  final Value<String> eventId;
  final Value<String> userId;
  final Value<String> eventType;
  final Value<String> storyId;
  final Value<int> storyVersion;
  final Value<DateTime> occurredAt;
  final Value<int> rowid;
  const GetBetterEventOutboxCompanion({
    this.eventId = const Value.absent(),
    this.userId = const Value.absent(),
    this.eventType = const Value.absent(),
    this.storyId = const Value.absent(),
    this.storyVersion = const Value.absent(),
    this.occurredAt = const Value.absent(),
    this.rowid = const Value.absent(),
  });
  GetBetterEventOutboxCompanion.insert({
    required String eventId,
    required String userId,
    required String eventType,
    required String storyId,
    required int storyVersion,
    required DateTime occurredAt,
    this.rowid = const Value.absent(),
  }) : eventId = Value(eventId),
       userId = Value(userId),
       eventType = Value(eventType),
       storyId = Value(storyId),
       storyVersion = Value(storyVersion),
       occurredAt = Value(occurredAt);
  static Insertable<GetBetterEventOutboxData> custom({
    Expression<String>? eventId,
    Expression<String>? userId,
    Expression<String>? eventType,
    Expression<String>? storyId,
    Expression<int>? storyVersion,
    Expression<DateTime>? occurredAt,
    Expression<int>? rowid,
  }) {
    return RawValuesInsertable({
      if (eventId != null) 'event_id': eventId,
      if (userId != null) 'user_id': userId,
      if (eventType != null) 'event_type': eventType,
      if (storyId != null) 'story_id': storyId,
      if (storyVersion != null) 'story_version': storyVersion,
      if (occurredAt != null) 'occurred_at': occurredAt,
      if (rowid != null) 'rowid': rowid,
    });
  }

  GetBetterEventOutboxCompanion copyWith({
    Value<String>? eventId,
    Value<String>? userId,
    Value<String>? eventType,
    Value<String>? storyId,
    Value<int>? storyVersion,
    Value<DateTime>? occurredAt,
    Value<int>? rowid,
  }) {
    return GetBetterEventOutboxCompanion(
      eventId: eventId ?? this.eventId,
      userId: userId ?? this.userId,
      eventType: eventType ?? this.eventType,
      storyId: storyId ?? this.storyId,
      storyVersion: storyVersion ?? this.storyVersion,
      occurredAt: occurredAt ?? this.occurredAt,
      rowid: rowid ?? this.rowid,
    );
  }

  @override
  Map<String, Expression> toColumns(bool nullToAbsent) {
    final map = <String, Expression>{};
    if (eventId.present) {
      map['event_id'] = Variable<String>(eventId.value);
    }
    if (userId.present) {
      map['user_id'] = Variable<String>(userId.value);
    }
    if (eventType.present) {
      map['event_type'] = Variable<String>(eventType.value);
    }
    if (storyId.present) {
      map['story_id'] = Variable<String>(storyId.value);
    }
    if (storyVersion.present) {
      map['story_version'] = Variable<int>(storyVersion.value);
    }
    if (occurredAt.present) {
      map['occurred_at'] = Variable<DateTime>(occurredAt.value);
    }
    if (rowid.present) {
      map['rowid'] = Variable<int>(rowid.value);
    }
    return map;
  }

  @override
  String toString() {
    return (StringBuffer('GetBetterEventOutboxCompanion(')
          ..write('eventId: $eventId, ')
          ..write('userId: $userId, ')
          ..write('eventType: $eventType, ')
          ..write('storyId: $storyId, ')
          ..write('storyVersion: $storyVersion, ')
          ..write('occurredAt: $occurredAt, ')
          ..write('rowid: $rowid')
          ..write(')'))
        .toString();
  }
}

abstract class _$AppDatabase extends GeneratedDatabase {
  _$AppDatabase(QueryExecutor e) : super(e);
  $AppDatabaseManager get managers => $AppDatabaseManager(this);
  late final $ChatSessionsTable chatSessions = $ChatSessionsTable(this);
  late final $ChatMessagesTable chatMessages = $ChatMessagesTable(this);
  late final $ChatSyncJobsTable chatSyncJobs = $ChatSyncJobsTable(this);
  late final $GetBetterCatalogCachesTable getBetterCatalogCaches =
      $GetBetterCatalogCachesTable(this);
  late final $GetBetterStoryProgressTable getBetterStoryProgress =
      $GetBetterStoryProgressTable(this);
  late final $GetBetterEventOutboxTable getBetterEventOutbox =
      $GetBetterEventOutboxTable(this);
  @override
  Iterable<TableInfo<Table, Object?>> get allTables =>
      allSchemaEntities.whereType<TableInfo<Table, Object?>>();
  @override
  List<DatabaseSchemaEntity> get allSchemaEntities => [
    chatSessions,
    chatMessages,
    chatSyncJobs,
    getBetterCatalogCaches,
    getBetterStoryProgress,
    getBetterEventOutbox,
  ];
  @override
  StreamQueryUpdateRules get streamUpdateRules => const StreamQueryUpdateRules([
    WritePropagation(
      on: TableUpdateQuery.onTableName(
        'chat_sessions',
        limitUpdateKind: UpdateKind.delete,
      ),
      result: [TableUpdate('chat_messages', kind: UpdateKind.delete)],
    ),
  ]);
}

typedef $$ChatSessionsTableCreateCompanionBuilder =
    ChatSessionsCompanion Function({
      required String id,
      Value<String> userId,
      required DateTime startedAt,
      Value<DateTime> updatedAt,
      Value<String?> title,
      Value<DateTime?> lastMessageAt,
      Value<String?> lastMessagePreview,
      Value<int> messageCount,
      Value<String?> agentId,
      Value<int> rowid,
    });
typedef $$ChatSessionsTableUpdateCompanionBuilder =
    ChatSessionsCompanion Function({
      Value<String> id,
      Value<String> userId,
      Value<DateTime> startedAt,
      Value<DateTime> updatedAt,
      Value<String?> title,
      Value<DateTime?> lastMessageAt,
      Value<String?> lastMessagePreview,
      Value<int> messageCount,
      Value<String?> agentId,
      Value<int> rowid,
    });

final class $$ChatSessionsTableReferences
    extends BaseReferences<_$AppDatabase, $ChatSessionsTable, ChatSession> {
  $$ChatSessionsTableReferences(super.$_db, super.$_table, super.$_typedResult);

  static MultiTypedResultKey<$ChatMessagesTable, List<ChatMessage>>
  _chatMessagesRefsTable(_$AppDatabase db) => MultiTypedResultKey.fromTable(
    db.chatMessages,
    aliasName: 'chat_sessions__id__chat_messages__session_id',
  );

  $$ChatMessagesTableProcessedTableManager get chatMessagesRefs {
    final manager = $$ChatMessagesTableTableManager(
      $_db,
      $_db.chatMessages,
    ).filter((f) => f.sessionId.id.sqlEquals($_itemColumn<String>('id')!));

    final cache = $_typedResult.readTableOrNull(_chatMessagesRefsTable($_db));
    return ProcessedTableManager(
      manager.$state.copyWith(prefetchedData: cache),
    );
  }
}

class $$ChatSessionsTableFilterComposer
    extends Composer<_$AppDatabase, $ChatSessionsTable> {
  $$ChatSessionsTableFilterComposer({
    required super.$db,
    required super.$table,
    super.joinBuilder,
    super.$addJoinBuilderToRootComposer,
    super.$removeJoinBuilderFromRootComposer,
  });
  ColumnFilters<String> get id => $composableBuilder(
    column: $table.id,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<String> get userId => $composableBuilder(
    column: $table.userId,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<DateTime> get startedAt => $composableBuilder(
    column: $table.startedAt,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<DateTime> get updatedAt => $composableBuilder(
    column: $table.updatedAt,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<String> get title => $composableBuilder(
    column: $table.title,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<DateTime> get lastMessageAt => $composableBuilder(
    column: $table.lastMessageAt,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<String> get lastMessagePreview => $composableBuilder(
    column: $table.lastMessagePreview,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<int> get messageCount => $composableBuilder(
    column: $table.messageCount,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<String> get agentId => $composableBuilder(
    column: $table.agentId,
    builder: (column) => ColumnFilters(column),
  );

  Expression<bool> chatMessagesRefs(
    Expression<bool> Function($$ChatMessagesTableFilterComposer f) f,
  ) {
    final $$ChatMessagesTableFilterComposer composer = $composerBuilder(
      composer: this,
      getCurrentColumn: (t) => t.id,
      referencedTable: $db.chatMessages,
      getReferencedColumn: (t) => t.sessionId,
      builder:
          (
            joinBuilder, {
            $addJoinBuilderToRootComposer,
            $removeJoinBuilderFromRootComposer,
          }) => $$ChatMessagesTableFilterComposer(
            $db: $db,
            $table: $db.chatMessages,
            $addJoinBuilderToRootComposer: $addJoinBuilderToRootComposer,
            joinBuilder: joinBuilder,
            $removeJoinBuilderFromRootComposer:
                $removeJoinBuilderFromRootComposer,
          ),
    );
    return f(composer);
  }
}

class $$ChatSessionsTableOrderingComposer
    extends Composer<_$AppDatabase, $ChatSessionsTable> {
  $$ChatSessionsTableOrderingComposer({
    required super.$db,
    required super.$table,
    super.joinBuilder,
    super.$addJoinBuilderToRootComposer,
    super.$removeJoinBuilderFromRootComposer,
  });
  ColumnOrderings<String> get id => $composableBuilder(
    column: $table.id,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<String> get userId => $composableBuilder(
    column: $table.userId,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<DateTime> get startedAt => $composableBuilder(
    column: $table.startedAt,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<DateTime> get updatedAt => $composableBuilder(
    column: $table.updatedAt,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<String> get title => $composableBuilder(
    column: $table.title,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<DateTime> get lastMessageAt => $composableBuilder(
    column: $table.lastMessageAt,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<String> get lastMessagePreview => $composableBuilder(
    column: $table.lastMessagePreview,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<int> get messageCount => $composableBuilder(
    column: $table.messageCount,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<String> get agentId => $composableBuilder(
    column: $table.agentId,
    builder: (column) => ColumnOrderings(column),
  );
}

class $$ChatSessionsTableAnnotationComposer
    extends Composer<_$AppDatabase, $ChatSessionsTable> {
  $$ChatSessionsTableAnnotationComposer({
    required super.$db,
    required super.$table,
    super.joinBuilder,
    super.$addJoinBuilderToRootComposer,
    super.$removeJoinBuilderFromRootComposer,
  });
  GeneratedColumn<String> get id =>
      $composableBuilder(column: $table.id, builder: (column) => column);

  GeneratedColumn<String> get userId =>
      $composableBuilder(column: $table.userId, builder: (column) => column);

  GeneratedColumn<DateTime> get startedAt =>
      $composableBuilder(column: $table.startedAt, builder: (column) => column);

  GeneratedColumn<DateTime> get updatedAt =>
      $composableBuilder(column: $table.updatedAt, builder: (column) => column);

  GeneratedColumn<String> get title =>
      $composableBuilder(column: $table.title, builder: (column) => column);

  GeneratedColumn<DateTime> get lastMessageAt => $composableBuilder(
    column: $table.lastMessageAt,
    builder: (column) => column,
  );

  GeneratedColumn<String> get lastMessagePreview => $composableBuilder(
    column: $table.lastMessagePreview,
    builder: (column) => column,
  );

  GeneratedColumn<int> get messageCount => $composableBuilder(
    column: $table.messageCount,
    builder: (column) => column,
  );

  GeneratedColumn<String> get agentId =>
      $composableBuilder(column: $table.agentId, builder: (column) => column);

  Expression<T> chatMessagesRefs<T extends Object>(
    Expression<T> Function($$ChatMessagesTableAnnotationComposer a) f,
  ) {
    final $$ChatMessagesTableAnnotationComposer composer = $composerBuilder(
      composer: this,
      getCurrentColumn: (t) => t.id,
      referencedTable: $db.chatMessages,
      getReferencedColumn: (t) => t.sessionId,
      builder:
          (
            joinBuilder, {
            $addJoinBuilderToRootComposer,
            $removeJoinBuilderFromRootComposer,
          }) => $$ChatMessagesTableAnnotationComposer(
            $db: $db,
            $table: $db.chatMessages,
            $addJoinBuilderToRootComposer: $addJoinBuilderToRootComposer,
            joinBuilder: joinBuilder,
            $removeJoinBuilderFromRootComposer:
                $removeJoinBuilderFromRootComposer,
          ),
    );
    return f(composer);
  }
}

class $$ChatSessionsTableTableManager
    extends
        RootTableManager<
          _$AppDatabase,
          $ChatSessionsTable,
          ChatSession,
          $$ChatSessionsTableFilterComposer,
          $$ChatSessionsTableOrderingComposer,
          $$ChatSessionsTableAnnotationComposer,
          $$ChatSessionsTableCreateCompanionBuilder,
          $$ChatSessionsTableUpdateCompanionBuilder,
          (ChatSession, $$ChatSessionsTableReferences),
          ChatSession,
          PrefetchHooks Function({bool chatMessagesRefs})
        > {
  $$ChatSessionsTableTableManager(_$AppDatabase db, $ChatSessionsTable table)
    : super(
        TableManagerState(
          db: db,
          table: table,
          createFilteringComposer: () =>
              $$ChatSessionsTableFilterComposer($db: db, $table: table),
          createOrderingComposer: () =>
              $$ChatSessionsTableOrderingComposer($db: db, $table: table),
          createComputedFieldComposer: () =>
              $$ChatSessionsTableAnnotationComposer($db: db, $table: table),
          updateCompanionCallback:
              ({
                Value<String> id = const Value.absent(),
                Value<String> userId = const Value.absent(),
                Value<DateTime> startedAt = const Value.absent(),
                Value<DateTime> updatedAt = const Value.absent(),
                Value<String?> title = const Value.absent(),
                Value<DateTime?> lastMessageAt = const Value.absent(),
                Value<String?> lastMessagePreview = const Value.absent(),
                Value<int> messageCount = const Value.absent(),
                Value<String?> agentId = const Value.absent(),
                Value<int> rowid = const Value.absent(),
              }) => ChatSessionsCompanion(
                id: id,
                userId: userId,
                startedAt: startedAt,
                updatedAt: updatedAt,
                title: title,
                lastMessageAt: lastMessageAt,
                lastMessagePreview: lastMessagePreview,
                messageCount: messageCount,
                agentId: agentId,
                rowid: rowid,
              ),
          createCompanionCallback:
              ({
                required String id,
                Value<String> userId = const Value.absent(),
                required DateTime startedAt,
                Value<DateTime> updatedAt = const Value.absent(),
                Value<String?> title = const Value.absent(),
                Value<DateTime?> lastMessageAt = const Value.absent(),
                Value<String?> lastMessagePreview = const Value.absent(),
                Value<int> messageCount = const Value.absent(),
                Value<String?> agentId = const Value.absent(),
                Value<int> rowid = const Value.absent(),
              }) => ChatSessionsCompanion.insert(
                id: id,
                userId: userId,
                startedAt: startedAt,
                updatedAt: updatedAt,
                title: title,
                lastMessageAt: lastMessageAt,
                lastMessagePreview: lastMessagePreview,
                messageCount: messageCount,
                agentId: agentId,
                rowid: rowid,
              ),
          withReferenceMapper: (p0) => p0
              .map(
                (e) => (
                  e.readTable(table),
                  $$ChatSessionsTableReferences(db, table, e),
                ),
              )
              .toList(),
          prefetchHooksCallback: ({chatMessagesRefs = false}) {
            return PrefetchHooks(
              db: db,
              explicitlyWatchedTables: [if (chatMessagesRefs) db.chatMessages],
              addJoins: null,
              getPrefetchedDataCallback: (items) async {
                return [
                  if (chatMessagesRefs)
                    await $_getPrefetchedData<
                      ChatSession,
                      $ChatSessionsTable,
                      ChatMessage
                    >(
                      currentTable: table,
                      referencedTable: $$ChatSessionsTableReferences
                          ._chatMessagesRefsTable(db),
                      managerFromTypedResult: (p0) =>
                          $$ChatSessionsTableReferences(
                            db,
                            table,
                            p0,
                          ).chatMessagesRefs,
                      referencedItemsForCurrentItem: (item, referencedItems) =>
                          referencedItems.where((e) => e.sessionId == item.id),
                      typedResults: items,
                    ),
                ];
              },
            );
          },
        ),
      );
}

typedef $$ChatSessionsTableProcessedTableManager =
    ProcessedTableManager<
      _$AppDatabase,
      $ChatSessionsTable,
      ChatSession,
      $$ChatSessionsTableFilterComposer,
      $$ChatSessionsTableOrderingComposer,
      $$ChatSessionsTableAnnotationComposer,
      $$ChatSessionsTableCreateCompanionBuilder,
      $$ChatSessionsTableUpdateCompanionBuilder,
      (ChatSession, $$ChatSessionsTableReferences),
      ChatSession,
      PrefetchHooks Function({bool chatMessagesRefs})
    >;
typedef $$ChatMessagesTableCreateCompanionBuilder =
    ChatMessagesCompanion Function({
      required String id,
      required String sessionId,
      required String content,
      required bool isUser,
      required String channel,
      required DateTime timestamp,
      Value<int> sequence,
      Value<String?> feedback,
      Value<String?> status,
      Value<String?> errorReason,
      Value<String?> engagementId,
      Value<String?> engagementAgent,
      Value<String?> reminderJson,
      Value<String?> clarificationJson,
      Value<String?> attachmentJson,
      Value<String?> inputMethod,
      Value<int> rowid,
    });
typedef $$ChatMessagesTableUpdateCompanionBuilder =
    ChatMessagesCompanion Function({
      Value<String> id,
      Value<String> sessionId,
      Value<String> content,
      Value<bool> isUser,
      Value<String> channel,
      Value<DateTime> timestamp,
      Value<int> sequence,
      Value<String?> feedback,
      Value<String?> status,
      Value<String?> errorReason,
      Value<String?> engagementId,
      Value<String?> engagementAgent,
      Value<String?> reminderJson,
      Value<String?> clarificationJson,
      Value<String?> attachmentJson,
      Value<String?> inputMethod,
      Value<int> rowid,
    });

final class $$ChatMessagesTableReferences
    extends BaseReferences<_$AppDatabase, $ChatMessagesTable, ChatMessage> {
  $$ChatMessagesTableReferences(super.$_db, super.$_table, super.$_typedResult);

  static $ChatSessionsTable _sessionIdTable(_$AppDatabase db) => db.chatSessions
      .createAlias('chat_messages__session_id__chat_sessions__id');

  $$ChatSessionsTableProcessedTableManager get sessionId {
    final $_column = $_itemColumn<String>('session_id')!;

    final manager = $$ChatSessionsTableTableManager(
      $_db,
      $_db.chatSessions,
    ).filter((f) => f.id.sqlEquals($_column));
    final item = $_typedResult.readTableOrNull(_sessionIdTable($_db));
    if (item == null) return manager;
    return ProcessedTableManager(
      manager.$state.copyWith(prefetchedData: [item]),
    );
  }
}

class $$ChatMessagesTableFilterComposer
    extends Composer<_$AppDatabase, $ChatMessagesTable> {
  $$ChatMessagesTableFilterComposer({
    required super.$db,
    required super.$table,
    super.joinBuilder,
    super.$addJoinBuilderToRootComposer,
    super.$removeJoinBuilderFromRootComposer,
  });
  ColumnFilters<String> get id => $composableBuilder(
    column: $table.id,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<String> get content => $composableBuilder(
    column: $table.content,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<bool> get isUser => $composableBuilder(
    column: $table.isUser,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<String> get channel => $composableBuilder(
    column: $table.channel,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<DateTime> get timestamp => $composableBuilder(
    column: $table.timestamp,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<int> get sequence => $composableBuilder(
    column: $table.sequence,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<String> get feedback => $composableBuilder(
    column: $table.feedback,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<String> get status => $composableBuilder(
    column: $table.status,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<String> get errorReason => $composableBuilder(
    column: $table.errorReason,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<String> get engagementId => $composableBuilder(
    column: $table.engagementId,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<String> get engagementAgent => $composableBuilder(
    column: $table.engagementAgent,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<String> get reminderJson => $composableBuilder(
    column: $table.reminderJson,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<String> get clarificationJson => $composableBuilder(
    column: $table.clarificationJson,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<String> get attachmentJson => $composableBuilder(
    column: $table.attachmentJson,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<String> get inputMethod => $composableBuilder(
    column: $table.inputMethod,
    builder: (column) => ColumnFilters(column),
  );

  $$ChatSessionsTableFilterComposer get sessionId {
    final $$ChatSessionsTableFilterComposer composer = $composerBuilder(
      composer: this,
      getCurrentColumn: (t) => t.sessionId,
      referencedTable: $db.chatSessions,
      getReferencedColumn: (t) => t.id,
      builder:
          (
            joinBuilder, {
            $addJoinBuilderToRootComposer,
            $removeJoinBuilderFromRootComposer,
          }) => $$ChatSessionsTableFilterComposer(
            $db: $db,
            $table: $db.chatSessions,
            $addJoinBuilderToRootComposer: $addJoinBuilderToRootComposer,
            joinBuilder: joinBuilder,
            $removeJoinBuilderFromRootComposer:
                $removeJoinBuilderFromRootComposer,
          ),
    );
    return composer;
  }
}

class $$ChatMessagesTableOrderingComposer
    extends Composer<_$AppDatabase, $ChatMessagesTable> {
  $$ChatMessagesTableOrderingComposer({
    required super.$db,
    required super.$table,
    super.joinBuilder,
    super.$addJoinBuilderToRootComposer,
    super.$removeJoinBuilderFromRootComposer,
  });
  ColumnOrderings<String> get id => $composableBuilder(
    column: $table.id,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<String> get content => $composableBuilder(
    column: $table.content,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<bool> get isUser => $composableBuilder(
    column: $table.isUser,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<String> get channel => $composableBuilder(
    column: $table.channel,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<DateTime> get timestamp => $composableBuilder(
    column: $table.timestamp,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<int> get sequence => $composableBuilder(
    column: $table.sequence,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<String> get feedback => $composableBuilder(
    column: $table.feedback,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<String> get status => $composableBuilder(
    column: $table.status,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<String> get errorReason => $composableBuilder(
    column: $table.errorReason,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<String> get engagementId => $composableBuilder(
    column: $table.engagementId,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<String> get engagementAgent => $composableBuilder(
    column: $table.engagementAgent,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<String> get reminderJson => $composableBuilder(
    column: $table.reminderJson,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<String> get clarificationJson => $composableBuilder(
    column: $table.clarificationJson,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<String> get attachmentJson => $composableBuilder(
    column: $table.attachmentJson,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<String> get inputMethod => $composableBuilder(
    column: $table.inputMethod,
    builder: (column) => ColumnOrderings(column),
  );

  $$ChatSessionsTableOrderingComposer get sessionId {
    final $$ChatSessionsTableOrderingComposer composer = $composerBuilder(
      composer: this,
      getCurrentColumn: (t) => t.sessionId,
      referencedTable: $db.chatSessions,
      getReferencedColumn: (t) => t.id,
      builder:
          (
            joinBuilder, {
            $addJoinBuilderToRootComposer,
            $removeJoinBuilderFromRootComposer,
          }) => $$ChatSessionsTableOrderingComposer(
            $db: $db,
            $table: $db.chatSessions,
            $addJoinBuilderToRootComposer: $addJoinBuilderToRootComposer,
            joinBuilder: joinBuilder,
            $removeJoinBuilderFromRootComposer:
                $removeJoinBuilderFromRootComposer,
          ),
    );
    return composer;
  }
}

class $$ChatMessagesTableAnnotationComposer
    extends Composer<_$AppDatabase, $ChatMessagesTable> {
  $$ChatMessagesTableAnnotationComposer({
    required super.$db,
    required super.$table,
    super.joinBuilder,
    super.$addJoinBuilderToRootComposer,
    super.$removeJoinBuilderFromRootComposer,
  });
  GeneratedColumn<String> get id =>
      $composableBuilder(column: $table.id, builder: (column) => column);

  GeneratedColumn<String> get content =>
      $composableBuilder(column: $table.content, builder: (column) => column);

  GeneratedColumn<bool> get isUser =>
      $composableBuilder(column: $table.isUser, builder: (column) => column);

  GeneratedColumn<String> get channel =>
      $composableBuilder(column: $table.channel, builder: (column) => column);

  GeneratedColumn<DateTime> get timestamp =>
      $composableBuilder(column: $table.timestamp, builder: (column) => column);

  GeneratedColumn<int> get sequence =>
      $composableBuilder(column: $table.sequence, builder: (column) => column);

  GeneratedColumn<String> get feedback =>
      $composableBuilder(column: $table.feedback, builder: (column) => column);

  GeneratedColumn<String> get status =>
      $composableBuilder(column: $table.status, builder: (column) => column);

  GeneratedColumn<String> get errorReason => $composableBuilder(
    column: $table.errorReason,
    builder: (column) => column,
  );

  GeneratedColumn<String> get engagementId => $composableBuilder(
    column: $table.engagementId,
    builder: (column) => column,
  );

  GeneratedColumn<String> get engagementAgent => $composableBuilder(
    column: $table.engagementAgent,
    builder: (column) => column,
  );

  GeneratedColumn<String> get reminderJson => $composableBuilder(
    column: $table.reminderJson,
    builder: (column) => column,
  );

  GeneratedColumn<String> get clarificationJson => $composableBuilder(
    column: $table.clarificationJson,
    builder: (column) => column,
  );

  GeneratedColumn<String> get attachmentJson => $composableBuilder(
    column: $table.attachmentJson,
    builder: (column) => column,
  );

  GeneratedColumn<String> get inputMethod => $composableBuilder(
    column: $table.inputMethod,
    builder: (column) => column,
  );

  $$ChatSessionsTableAnnotationComposer get sessionId {
    final $$ChatSessionsTableAnnotationComposer composer = $composerBuilder(
      composer: this,
      getCurrentColumn: (t) => t.sessionId,
      referencedTable: $db.chatSessions,
      getReferencedColumn: (t) => t.id,
      builder:
          (
            joinBuilder, {
            $addJoinBuilderToRootComposer,
            $removeJoinBuilderFromRootComposer,
          }) => $$ChatSessionsTableAnnotationComposer(
            $db: $db,
            $table: $db.chatSessions,
            $addJoinBuilderToRootComposer: $addJoinBuilderToRootComposer,
            joinBuilder: joinBuilder,
            $removeJoinBuilderFromRootComposer:
                $removeJoinBuilderFromRootComposer,
          ),
    );
    return composer;
  }
}

class $$ChatMessagesTableTableManager
    extends
        RootTableManager<
          _$AppDatabase,
          $ChatMessagesTable,
          ChatMessage,
          $$ChatMessagesTableFilterComposer,
          $$ChatMessagesTableOrderingComposer,
          $$ChatMessagesTableAnnotationComposer,
          $$ChatMessagesTableCreateCompanionBuilder,
          $$ChatMessagesTableUpdateCompanionBuilder,
          (ChatMessage, $$ChatMessagesTableReferences),
          ChatMessage,
          PrefetchHooks Function({bool sessionId})
        > {
  $$ChatMessagesTableTableManager(_$AppDatabase db, $ChatMessagesTable table)
    : super(
        TableManagerState(
          db: db,
          table: table,
          createFilteringComposer: () =>
              $$ChatMessagesTableFilterComposer($db: db, $table: table),
          createOrderingComposer: () =>
              $$ChatMessagesTableOrderingComposer($db: db, $table: table),
          createComputedFieldComposer: () =>
              $$ChatMessagesTableAnnotationComposer($db: db, $table: table),
          updateCompanionCallback:
              ({
                Value<String> id = const Value.absent(),
                Value<String> sessionId = const Value.absent(),
                Value<String> content = const Value.absent(),
                Value<bool> isUser = const Value.absent(),
                Value<String> channel = const Value.absent(),
                Value<DateTime> timestamp = const Value.absent(),
                Value<int> sequence = const Value.absent(),
                Value<String?> feedback = const Value.absent(),
                Value<String?> status = const Value.absent(),
                Value<String?> errorReason = const Value.absent(),
                Value<String?> engagementId = const Value.absent(),
                Value<String?> engagementAgent = const Value.absent(),
                Value<String?> reminderJson = const Value.absent(),
                Value<String?> clarificationJson = const Value.absent(),
                Value<String?> attachmentJson = const Value.absent(),
                Value<String?> inputMethod = const Value.absent(),
                Value<int> rowid = const Value.absent(),
              }) => ChatMessagesCompanion(
                id: id,
                sessionId: sessionId,
                content: content,
                isUser: isUser,
                channel: channel,
                timestamp: timestamp,
                sequence: sequence,
                feedback: feedback,
                status: status,
                errorReason: errorReason,
                engagementId: engagementId,
                engagementAgent: engagementAgent,
                reminderJson: reminderJson,
                clarificationJson: clarificationJson,
                attachmentJson: attachmentJson,
                inputMethod: inputMethod,
                rowid: rowid,
              ),
          createCompanionCallback:
              ({
                required String id,
                required String sessionId,
                required String content,
                required bool isUser,
                required String channel,
                required DateTime timestamp,
                Value<int> sequence = const Value.absent(),
                Value<String?> feedback = const Value.absent(),
                Value<String?> status = const Value.absent(),
                Value<String?> errorReason = const Value.absent(),
                Value<String?> engagementId = const Value.absent(),
                Value<String?> engagementAgent = const Value.absent(),
                Value<String?> reminderJson = const Value.absent(),
                Value<String?> clarificationJson = const Value.absent(),
                Value<String?> attachmentJson = const Value.absent(),
                Value<String?> inputMethod = const Value.absent(),
                Value<int> rowid = const Value.absent(),
              }) => ChatMessagesCompanion.insert(
                id: id,
                sessionId: sessionId,
                content: content,
                isUser: isUser,
                channel: channel,
                timestamp: timestamp,
                sequence: sequence,
                feedback: feedback,
                status: status,
                errorReason: errorReason,
                engagementId: engagementId,
                engagementAgent: engagementAgent,
                reminderJson: reminderJson,
                clarificationJson: clarificationJson,
                attachmentJson: attachmentJson,
                inputMethod: inputMethod,
                rowid: rowid,
              ),
          withReferenceMapper: (p0) => p0
              .map(
                (e) => (
                  e.readTable(table),
                  $$ChatMessagesTableReferences(db, table, e),
                ),
              )
              .toList(),
          prefetchHooksCallback: ({sessionId = false}) {
            return PrefetchHooks(
              db: db,
              explicitlyWatchedTables: [],
              addJoins:
                  <
                    T extends TableManagerState<
                      dynamic,
                      dynamic,
                      dynamic,
                      dynamic,
                      dynamic,
                      dynamic,
                      dynamic,
                      dynamic,
                      dynamic,
                      dynamic,
                      dynamic
                    >
                  >(state) {
                    if (sessionId) {
                      state =
                          state.withJoin(
                                currentTable: table,
                                currentColumn: table.sessionId,
                                referencedTable: $$ChatMessagesTableReferences
                                    ._sessionIdTable(db),
                                referencedColumn: $$ChatMessagesTableReferences
                                    ._sessionIdTable(db)
                                    .id,
                              )
                              as T;
                    }

                    return state;
                  },
              getPrefetchedDataCallback: (items) async {
                return [];
              },
            );
          },
        ),
      );
}

typedef $$ChatMessagesTableProcessedTableManager =
    ProcessedTableManager<
      _$AppDatabase,
      $ChatMessagesTable,
      ChatMessage,
      $$ChatMessagesTableFilterComposer,
      $$ChatMessagesTableOrderingComposer,
      $$ChatMessagesTableAnnotationComposer,
      $$ChatMessagesTableCreateCompanionBuilder,
      $$ChatMessagesTableUpdateCompanionBuilder,
      (ChatMessage, $$ChatMessagesTableReferences),
      ChatMessage,
      PrefetchHooks Function({bool sessionId})
    >;
typedef $$ChatSyncJobsTableCreateCompanionBuilder =
    ChatSyncJobsCompanion Function({
      Value<int> id,
      required String userId,
      required String sessionId,
      Value<String?> messageId,
      required String jobType,
      Value<DateTime> createdAt,
      Value<DateTime> nextAttemptAt,
      Value<int> attemptCount,
      Value<String?> lastError,
    });
typedef $$ChatSyncJobsTableUpdateCompanionBuilder =
    ChatSyncJobsCompanion Function({
      Value<int> id,
      Value<String> userId,
      Value<String> sessionId,
      Value<String?> messageId,
      Value<String> jobType,
      Value<DateTime> createdAt,
      Value<DateTime> nextAttemptAt,
      Value<int> attemptCount,
      Value<String?> lastError,
    });

class $$ChatSyncJobsTableFilterComposer
    extends Composer<_$AppDatabase, $ChatSyncJobsTable> {
  $$ChatSyncJobsTableFilterComposer({
    required super.$db,
    required super.$table,
    super.joinBuilder,
    super.$addJoinBuilderToRootComposer,
    super.$removeJoinBuilderFromRootComposer,
  });
  ColumnFilters<int> get id => $composableBuilder(
    column: $table.id,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<String> get userId => $composableBuilder(
    column: $table.userId,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<String> get sessionId => $composableBuilder(
    column: $table.sessionId,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<String> get messageId => $composableBuilder(
    column: $table.messageId,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<String> get jobType => $composableBuilder(
    column: $table.jobType,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<DateTime> get createdAt => $composableBuilder(
    column: $table.createdAt,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<DateTime> get nextAttemptAt => $composableBuilder(
    column: $table.nextAttemptAt,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<int> get attemptCount => $composableBuilder(
    column: $table.attemptCount,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<String> get lastError => $composableBuilder(
    column: $table.lastError,
    builder: (column) => ColumnFilters(column),
  );
}

class $$ChatSyncJobsTableOrderingComposer
    extends Composer<_$AppDatabase, $ChatSyncJobsTable> {
  $$ChatSyncJobsTableOrderingComposer({
    required super.$db,
    required super.$table,
    super.joinBuilder,
    super.$addJoinBuilderToRootComposer,
    super.$removeJoinBuilderFromRootComposer,
  });
  ColumnOrderings<int> get id => $composableBuilder(
    column: $table.id,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<String> get userId => $composableBuilder(
    column: $table.userId,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<String> get sessionId => $composableBuilder(
    column: $table.sessionId,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<String> get messageId => $composableBuilder(
    column: $table.messageId,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<String> get jobType => $composableBuilder(
    column: $table.jobType,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<DateTime> get createdAt => $composableBuilder(
    column: $table.createdAt,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<DateTime> get nextAttemptAt => $composableBuilder(
    column: $table.nextAttemptAt,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<int> get attemptCount => $composableBuilder(
    column: $table.attemptCount,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<String> get lastError => $composableBuilder(
    column: $table.lastError,
    builder: (column) => ColumnOrderings(column),
  );
}

class $$ChatSyncJobsTableAnnotationComposer
    extends Composer<_$AppDatabase, $ChatSyncJobsTable> {
  $$ChatSyncJobsTableAnnotationComposer({
    required super.$db,
    required super.$table,
    super.joinBuilder,
    super.$addJoinBuilderToRootComposer,
    super.$removeJoinBuilderFromRootComposer,
  });
  GeneratedColumn<int> get id =>
      $composableBuilder(column: $table.id, builder: (column) => column);

  GeneratedColumn<String> get userId =>
      $composableBuilder(column: $table.userId, builder: (column) => column);

  GeneratedColumn<String> get sessionId =>
      $composableBuilder(column: $table.sessionId, builder: (column) => column);

  GeneratedColumn<String> get messageId =>
      $composableBuilder(column: $table.messageId, builder: (column) => column);

  GeneratedColumn<String> get jobType =>
      $composableBuilder(column: $table.jobType, builder: (column) => column);

  GeneratedColumn<DateTime> get createdAt =>
      $composableBuilder(column: $table.createdAt, builder: (column) => column);

  GeneratedColumn<DateTime> get nextAttemptAt => $composableBuilder(
    column: $table.nextAttemptAt,
    builder: (column) => column,
  );

  GeneratedColumn<int> get attemptCount => $composableBuilder(
    column: $table.attemptCount,
    builder: (column) => column,
  );

  GeneratedColumn<String> get lastError =>
      $composableBuilder(column: $table.lastError, builder: (column) => column);
}

class $$ChatSyncJobsTableTableManager
    extends
        RootTableManager<
          _$AppDatabase,
          $ChatSyncJobsTable,
          ChatSyncJob,
          $$ChatSyncJobsTableFilterComposer,
          $$ChatSyncJobsTableOrderingComposer,
          $$ChatSyncJobsTableAnnotationComposer,
          $$ChatSyncJobsTableCreateCompanionBuilder,
          $$ChatSyncJobsTableUpdateCompanionBuilder,
          (
            ChatSyncJob,
            BaseReferences<_$AppDatabase, $ChatSyncJobsTable, ChatSyncJob>,
          ),
          ChatSyncJob,
          PrefetchHooks Function()
        > {
  $$ChatSyncJobsTableTableManager(_$AppDatabase db, $ChatSyncJobsTable table)
    : super(
        TableManagerState(
          db: db,
          table: table,
          createFilteringComposer: () =>
              $$ChatSyncJobsTableFilterComposer($db: db, $table: table),
          createOrderingComposer: () =>
              $$ChatSyncJobsTableOrderingComposer($db: db, $table: table),
          createComputedFieldComposer: () =>
              $$ChatSyncJobsTableAnnotationComposer($db: db, $table: table),
          updateCompanionCallback:
              ({
                Value<int> id = const Value.absent(),
                Value<String> userId = const Value.absent(),
                Value<String> sessionId = const Value.absent(),
                Value<String?> messageId = const Value.absent(),
                Value<String> jobType = const Value.absent(),
                Value<DateTime> createdAt = const Value.absent(),
                Value<DateTime> nextAttemptAt = const Value.absent(),
                Value<int> attemptCount = const Value.absent(),
                Value<String?> lastError = const Value.absent(),
              }) => ChatSyncJobsCompanion(
                id: id,
                userId: userId,
                sessionId: sessionId,
                messageId: messageId,
                jobType: jobType,
                createdAt: createdAt,
                nextAttemptAt: nextAttemptAt,
                attemptCount: attemptCount,
                lastError: lastError,
              ),
          createCompanionCallback:
              ({
                Value<int> id = const Value.absent(),
                required String userId,
                required String sessionId,
                Value<String?> messageId = const Value.absent(),
                required String jobType,
                Value<DateTime> createdAt = const Value.absent(),
                Value<DateTime> nextAttemptAt = const Value.absent(),
                Value<int> attemptCount = const Value.absent(),
                Value<String?> lastError = const Value.absent(),
              }) => ChatSyncJobsCompanion.insert(
                id: id,
                userId: userId,
                sessionId: sessionId,
                messageId: messageId,
                jobType: jobType,
                createdAt: createdAt,
                nextAttemptAt: nextAttemptAt,
                attemptCount: attemptCount,
                lastError: lastError,
              ),
          withReferenceMapper: (p0) => p0
              .map((e) => (e.readTable(table), BaseReferences(db, table, e)))
              .toList(),
          prefetchHooksCallback: null,
        ),
      );
}

typedef $$ChatSyncJobsTableProcessedTableManager =
    ProcessedTableManager<
      _$AppDatabase,
      $ChatSyncJobsTable,
      ChatSyncJob,
      $$ChatSyncJobsTableFilterComposer,
      $$ChatSyncJobsTableOrderingComposer,
      $$ChatSyncJobsTableAnnotationComposer,
      $$ChatSyncJobsTableCreateCompanionBuilder,
      $$ChatSyncJobsTableUpdateCompanionBuilder,
      (
        ChatSyncJob,
        BaseReferences<_$AppDatabase, $ChatSyncJobsTable, ChatSyncJob>,
      ),
      ChatSyncJob,
      PrefetchHooks Function()
    >;
typedef $$GetBetterCatalogCachesTableCreateCompanionBuilder =
    GetBetterCatalogCachesCompanion Function({
      required String cacheKey,
      required String catalogVersion,
      required String feedJson,
      required DateTime checkedAt,
      required DateTime updatedAt,
      Value<int> rowid,
    });
typedef $$GetBetterCatalogCachesTableUpdateCompanionBuilder =
    GetBetterCatalogCachesCompanion Function({
      Value<String> cacheKey,
      Value<String> catalogVersion,
      Value<String> feedJson,
      Value<DateTime> checkedAt,
      Value<DateTime> updatedAt,
      Value<int> rowid,
    });

class $$GetBetterCatalogCachesTableFilterComposer
    extends Composer<_$AppDatabase, $GetBetterCatalogCachesTable> {
  $$GetBetterCatalogCachesTableFilterComposer({
    required super.$db,
    required super.$table,
    super.joinBuilder,
    super.$addJoinBuilderToRootComposer,
    super.$removeJoinBuilderFromRootComposer,
  });
  ColumnFilters<String> get cacheKey => $composableBuilder(
    column: $table.cacheKey,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<String> get catalogVersion => $composableBuilder(
    column: $table.catalogVersion,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<String> get feedJson => $composableBuilder(
    column: $table.feedJson,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<DateTime> get checkedAt => $composableBuilder(
    column: $table.checkedAt,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<DateTime> get updatedAt => $composableBuilder(
    column: $table.updatedAt,
    builder: (column) => ColumnFilters(column),
  );
}

class $$GetBetterCatalogCachesTableOrderingComposer
    extends Composer<_$AppDatabase, $GetBetterCatalogCachesTable> {
  $$GetBetterCatalogCachesTableOrderingComposer({
    required super.$db,
    required super.$table,
    super.joinBuilder,
    super.$addJoinBuilderToRootComposer,
    super.$removeJoinBuilderFromRootComposer,
  });
  ColumnOrderings<String> get cacheKey => $composableBuilder(
    column: $table.cacheKey,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<String> get catalogVersion => $composableBuilder(
    column: $table.catalogVersion,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<String> get feedJson => $composableBuilder(
    column: $table.feedJson,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<DateTime> get checkedAt => $composableBuilder(
    column: $table.checkedAt,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<DateTime> get updatedAt => $composableBuilder(
    column: $table.updatedAt,
    builder: (column) => ColumnOrderings(column),
  );
}

class $$GetBetterCatalogCachesTableAnnotationComposer
    extends Composer<_$AppDatabase, $GetBetterCatalogCachesTable> {
  $$GetBetterCatalogCachesTableAnnotationComposer({
    required super.$db,
    required super.$table,
    super.joinBuilder,
    super.$addJoinBuilderToRootComposer,
    super.$removeJoinBuilderFromRootComposer,
  });
  GeneratedColumn<String> get cacheKey =>
      $composableBuilder(column: $table.cacheKey, builder: (column) => column);

  GeneratedColumn<String> get catalogVersion => $composableBuilder(
    column: $table.catalogVersion,
    builder: (column) => column,
  );

  GeneratedColumn<String> get feedJson =>
      $composableBuilder(column: $table.feedJson, builder: (column) => column);

  GeneratedColumn<DateTime> get checkedAt =>
      $composableBuilder(column: $table.checkedAt, builder: (column) => column);

  GeneratedColumn<DateTime> get updatedAt =>
      $composableBuilder(column: $table.updatedAt, builder: (column) => column);
}

class $$GetBetterCatalogCachesTableTableManager
    extends
        RootTableManager<
          _$AppDatabase,
          $GetBetterCatalogCachesTable,
          GetBetterCatalogCache,
          $$GetBetterCatalogCachesTableFilterComposer,
          $$GetBetterCatalogCachesTableOrderingComposer,
          $$GetBetterCatalogCachesTableAnnotationComposer,
          $$GetBetterCatalogCachesTableCreateCompanionBuilder,
          $$GetBetterCatalogCachesTableUpdateCompanionBuilder,
          (
            GetBetterCatalogCache,
            BaseReferences<
              _$AppDatabase,
              $GetBetterCatalogCachesTable,
              GetBetterCatalogCache
            >,
          ),
          GetBetterCatalogCache,
          PrefetchHooks Function()
        > {
  $$GetBetterCatalogCachesTableTableManager(
    _$AppDatabase db,
    $GetBetterCatalogCachesTable table,
  ) : super(
        TableManagerState(
          db: db,
          table: table,
          createFilteringComposer: () =>
              $$GetBetterCatalogCachesTableFilterComposer(
                $db: db,
                $table: table,
              ),
          createOrderingComposer: () =>
              $$GetBetterCatalogCachesTableOrderingComposer(
                $db: db,
                $table: table,
              ),
          createComputedFieldComposer: () =>
              $$GetBetterCatalogCachesTableAnnotationComposer(
                $db: db,
                $table: table,
              ),
          updateCompanionCallback:
              ({
                Value<String> cacheKey = const Value.absent(),
                Value<String> catalogVersion = const Value.absent(),
                Value<String> feedJson = const Value.absent(),
                Value<DateTime> checkedAt = const Value.absent(),
                Value<DateTime> updatedAt = const Value.absent(),
                Value<int> rowid = const Value.absent(),
              }) => GetBetterCatalogCachesCompanion(
                cacheKey: cacheKey,
                catalogVersion: catalogVersion,
                feedJson: feedJson,
                checkedAt: checkedAt,
                updatedAt: updatedAt,
                rowid: rowid,
              ),
          createCompanionCallback:
              ({
                required String cacheKey,
                required String catalogVersion,
                required String feedJson,
                required DateTime checkedAt,
                required DateTime updatedAt,
                Value<int> rowid = const Value.absent(),
              }) => GetBetterCatalogCachesCompanion.insert(
                cacheKey: cacheKey,
                catalogVersion: catalogVersion,
                feedJson: feedJson,
                checkedAt: checkedAt,
                updatedAt: updatedAt,
                rowid: rowid,
              ),
          withReferenceMapper: (p0) => p0
              .map((e) => (e.readTable(table), BaseReferences(db, table, e)))
              .toList(),
          prefetchHooksCallback: null,
        ),
      );
}

typedef $$GetBetterCatalogCachesTableProcessedTableManager =
    ProcessedTableManager<
      _$AppDatabase,
      $GetBetterCatalogCachesTable,
      GetBetterCatalogCache,
      $$GetBetterCatalogCachesTableFilterComposer,
      $$GetBetterCatalogCachesTableOrderingComposer,
      $$GetBetterCatalogCachesTableAnnotationComposer,
      $$GetBetterCatalogCachesTableCreateCompanionBuilder,
      $$GetBetterCatalogCachesTableUpdateCompanionBuilder,
      (
        GetBetterCatalogCache,
        BaseReferences<
          _$AppDatabase,
          $GetBetterCatalogCachesTable,
          GetBetterCatalogCache
        >,
      ),
      GetBetterCatalogCache,
      PrefetchHooks Function()
    >;
typedef $$GetBetterStoryProgressTableCreateCompanionBuilder =
    GetBetterStoryProgressCompanion Function({
      required String userId,
      required String storyId,
      Value<int> storyVersion,
      Value<bool> opened,
      Value<bool> saved,
      Value<bool> completed,
      Value<DateTime?> lastOpenedAt,
      Value<DateTime> updatedAt,
      Value<int> rowid,
    });
typedef $$GetBetterStoryProgressTableUpdateCompanionBuilder =
    GetBetterStoryProgressCompanion Function({
      Value<String> userId,
      Value<String> storyId,
      Value<int> storyVersion,
      Value<bool> opened,
      Value<bool> saved,
      Value<bool> completed,
      Value<DateTime?> lastOpenedAt,
      Value<DateTime> updatedAt,
      Value<int> rowid,
    });

class $$GetBetterStoryProgressTableFilterComposer
    extends Composer<_$AppDatabase, $GetBetterStoryProgressTable> {
  $$GetBetterStoryProgressTableFilterComposer({
    required super.$db,
    required super.$table,
    super.joinBuilder,
    super.$addJoinBuilderToRootComposer,
    super.$removeJoinBuilderFromRootComposer,
  });
  ColumnFilters<String> get userId => $composableBuilder(
    column: $table.userId,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<String> get storyId => $composableBuilder(
    column: $table.storyId,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<int> get storyVersion => $composableBuilder(
    column: $table.storyVersion,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<bool> get opened => $composableBuilder(
    column: $table.opened,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<bool> get saved => $composableBuilder(
    column: $table.saved,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<bool> get completed => $composableBuilder(
    column: $table.completed,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<DateTime> get lastOpenedAt => $composableBuilder(
    column: $table.lastOpenedAt,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<DateTime> get updatedAt => $composableBuilder(
    column: $table.updatedAt,
    builder: (column) => ColumnFilters(column),
  );
}

class $$GetBetterStoryProgressTableOrderingComposer
    extends Composer<_$AppDatabase, $GetBetterStoryProgressTable> {
  $$GetBetterStoryProgressTableOrderingComposer({
    required super.$db,
    required super.$table,
    super.joinBuilder,
    super.$addJoinBuilderToRootComposer,
    super.$removeJoinBuilderFromRootComposer,
  });
  ColumnOrderings<String> get userId => $composableBuilder(
    column: $table.userId,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<String> get storyId => $composableBuilder(
    column: $table.storyId,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<int> get storyVersion => $composableBuilder(
    column: $table.storyVersion,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<bool> get opened => $composableBuilder(
    column: $table.opened,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<bool> get saved => $composableBuilder(
    column: $table.saved,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<bool> get completed => $composableBuilder(
    column: $table.completed,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<DateTime> get lastOpenedAt => $composableBuilder(
    column: $table.lastOpenedAt,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<DateTime> get updatedAt => $composableBuilder(
    column: $table.updatedAt,
    builder: (column) => ColumnOrderings(column),
  );
}

class $$GetBetterStoryProgressTableAnnotationComposer
    extends Composer<_$AppDatabase, $GetBetterStoryProgressTable> {
  $$GetBetterStoryProgressTableAnnotationComposer({
    required super.$db,
    required super.$table,
    super.joinBuilder,
    super.$addJoinBuilderToRootComposer,
    super.$removeJoinBuilderFromRootComposer,
  });
  GeneratedColumn<String> get userId =>
      $composableBuilder(column: $table.userId, builder: (column) => column);

  GeneratedColumn<String> get storyId =>
      $composableBuilder(column: $table.storyId, builder: (column) => column);

  GeneratedColumn<int> get storyVersion => $composableBuilder(
    column: $table.storyVersion,
    builder: (column) => column,
  );

  GeneratedColumn<bool> get opened =>
      $composableBuilder(column: $table.opened, builder: (column) => column);

  GeneratedColumn<bool> get saved =>
      $composableBuilder(column: $table.saved, builder: (column) => column);

  GeneratedColumn<bool> get completed =>
      $composableBuilder(column: $table.completed, builder: (column) => column);

  GeneratedColumn<DateTime> get lastOpenedAt => $composableBuilder(
    column: $table.lastOpenedAt,
    builder: (column) => column,
  );

  GeneratedColumn<DateTime> get updatedAt =>
      $composableBuilder(column: $table.updatedAt, builder: (column) => column);
}

class $$GetBetterStoryProgressTableTableManager
    extends
        RootTableManager<
          _$AppDatabase,
          $GetBetterStoryProgressTable,
          GetBetterStoryProgressData,
          $$GetBetterStoryProgressTableFilterComposer,
          $$GetBetterStoryProgressTableOrderingComposer,
          $$GetBetterStoryProgressTableAnnotationComposer,
          $$GetBetterStoryProgressTableCreateCompanionBuilder,
          $$GetBetterStoryProgressTableUpdateCompanionBuilder,
          (
            GetBetterStoryProgressData,
            BaseReferences<
              _$AppDatabase,
              $GetBetterStoryProgressTable,
              GetBetterStoryProgressData
            >,
          ),
          GetBetterStoryProgressData,
          PrefetchHooks Function()
        > {
  $$GetBetterStoryProgressTableTableManager(
    _$AppDatabase db,
    $GetBetterStoryProgressTable table,
  ) : super(
        TableManagerState(
          db: db,
          table: table,
          createFilteringComposer: () =>
              $$GetBetterStoryProgressTableFilterComposer(
                $db: db,
                $table: table,
              ),
          createOrderingComposer: () =>
              $$GetBetterStoryProgressTableOrderingComposer(
                $db: db,
                $table: table,
              ),
          createComputedFieldComposer: () =>
              $$GetBetterStoryProgressTableAnnotationComposer(
                $db: db,
                $table: table,
              ),
          updateCompanionCallback:
              ({
                Value<String> userId = const Value.absent(),
                Value<String> storyId = const Value.absent(),
                Value<int> storyVersion = const Value.absent(),
                Value<bool> opened = const Value.absent(),
                Value<bool> saved = const Value.absent(),
                Value<bool> completed = const Value.absent(),
                Value<DateTime?> lastOpenedAt = const Value.absent(),
                Value<DateTime> updatedAt = const Value.absent(),
                Value<int> rowid = const Value.absent(),
              }) => GetBetterStoryProgressCompanion(
                userId: userId,
                storyId: storyId,
                storyVersion: storyVersion,
                opened: opened,
                saved: saved,
                completed: completed,
                lastOpenedAt: lastOpenedAt,
                updatedAt: updatedAt,
                rowid: rowid,
              ),
          createCompanionCallback:
              ({
                required String userId,
                required String storyId,
                Value<int> storyVersion = const Value.absent(),
                Value<bool> opened = const Value.absent(),
                Value<bool> saved = const Value.absent(),
                Value<bool> completed = const Value.absent(),
                Value<DateTime?> lastOpenedAt = const Value.absent(),
                Value<DateTime> updatedAt = const Value.absent(),
                Value<int> rowid = const Value.absent(),
              }) => GetBetterStoryProgressCompanion.insert(
                userId: userId,
                storyId: storyId,
                storyVersion: storyVersion,
                opened: opened,
                saved: saved,
                completed: completed,
                lastOpenedAt: lastOpenedAt,
                updatedAt: updatedAt,
                rowid: rowid,
              ),
          withReferenceMapper: (p0) => p0
              .map((e) => (e.readTable(table), BaseReferences(db, table, e)))
              .toList(),
          prefetchHooksCallback: null,
        ),
      );
}

typedef $$GetBetterStoryProgressTableProcessedTableManager =
    ProcessedTableManager<
      _$AppDatabase,
      $GetBetterStoryProgressTable,
      GetBetterStoryProgressData,
      $$GetBetterStoryProgressTableFilterComposer,
      $$GetBetterStoryProgressTableOrderingComposer,
      $$GetBetterStoryProgressTableAnnotationComposer,
      $$GetBetterStoryProgressTableCreateCompanionBuilder,
      $$GetBetterStoryProgressTableUpdateCompanionBuilder,
      (
        GetBetterStoryProgressData,
        BaseReferences<
          _$AppDatabase,
          $GetBetterStoryProgressTable,
          GetBetterStoryProgressData
        >,
      ),
      GetBetterStoryProgressData,
      PrefetchHooks Function()
    >;
typedef $$GetBetterEventOutboxTableCreateCompanionBuilder =
    GetBetterEventOutboxCompanion Function({
      required String eventId,
      required String userId,
      required String eventType,
      required String storyId,
      required int storyVersion,
      required DateTime occurredAt,
      Value<int> rowid,
    });
typedef $$GetBetterEventOutboxTableUpdateCompanionBuilder =
    GetBetterEventOutboxCompanion Function({
      Value<String> eventId,
      Value<String> userId,
      Value<String> eventType,
      Value<String> storyId,
      Value<int> storyVersion,
      Value<DateTime> occurredAt,
      Value<int> rowid,
    });

class $$GetBetterEventOutboxTableFilterComposer
    extends Composer<_$AppDatabase, $GetBetterEventOutboxTable> {
  $$GetBetterEventOutboxTableFilterComposer({
    required super.$db,
    required super.$table,
    super.joinBuilder,
    super.$addJoinBuilderToRootComposer,
    super.$removeJoinBuilderFromRootComposer,
  });
  ColumnFilters<String> get eventId => $composableBuilder(
    column: $table.eventId,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<String> get userId => $composableBuilder(
    column: $table.userId,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<String> get eventType => $composableBuilder(
    column: $table.eventType,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<String> get storyId => $composableBuilder(
    column: $table.storyId,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<int> get storyVersion => $composableBuilder(
    column: $table.storyVersion,
    builder: (column) => ColumnFilters(column),
  );

  ColumnFilters<DateTime> get occurredAt => $composableBuilder(
    column: $table.occurredAt,
    builder: (column) => ColumnFilters(column),
  );
}

class $$GetBetterEventOutboxTableOrderingComposer
    extends Composer<_$AppDatabase, $GetBetterEventOutboxTable> {
  $$GetBetterEventOutboxTableOrderingComposer({
    required super.$db,
    required super.$table,
    super.joinBuilder,
    super.$addJoinBuilderToRootComposer,
    super.$removeJoinBuilderFromRootComposer,
  });
  ColumnOrderings<String> get eventId => $composableBuilder(
    column: $table.eventId,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<String> get userId => $composableBuilder(
    column: $table.userId,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<String> get eventType => $composableBuilder(
    column: $table.eventType,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<String> get storyId => $composableBuilder(
    column: $table.storyId,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<int> get storyVersion => $composableBuilder(
    column: $table.storyVersion,
    builder: (column) => ColumnOrderings(column),
  );

  ColumnOrderings<DateTime> get occurredAt => $composableBuilder(
    column: $table.occurredAt,
    builder: (column) => ColumnOrderings(column),
  );
}

class $$GetBetterEventOutboxTableAnnotationComposer
    extends Composer<_$AppDatabase, $GetBetterEventOutboxTable> {
  $$GetBetterEventOutboxTableAnnotationComposer({
    required super.$db,
    required super.$table,
    super.joinBuilder,
    super.$addJoinBuilderToRootComposer,
    super.$removeJoinBuilderFromRootComposer,
  });
  GeneratedColumn<String> get eventId =>
      $composableBuilder(column: $table.eventId, builder: (column) => column);

  GeneratedColumn<String> get userId =>
      $composableBuilder(column: $table.userId, builder: (column) => column);

  GeneratedColumn<String> get eventType =>
      $composableBuilder(column: $table.eventType, builder: (column) => column);

  GeneratedColumn<String> get storyId =>
      $composableBuilder(column: $table.storyId, builder: (column) => column);

  GeneratedColumn<int> get storyVersion => $composableBuilder(
    column: $table.storyVersion,
    builder: (column) => column,
  );

  GeneratedColumn<DateTime> get occurredAt => $composableBuilder(
    column: $table.occurredAt,
    builder: (column) => column,
  );
}

class $$GetBetterEventOutboxTableTableManager
    extends
        RootTableManager<
          _$AppDatabase,
          $GetBetterEventOutboxTable,
          GetBetterEventOutboxData,
          $$GetBetterEventOutboxTableFilterComposer,
          $$GetBetterEventOutboxTableOrderingComposer,
          $$GetBetterEventOutboxTableAnnotationComposer,
          $$GetBetterEventOutboxTableCreateCompanionBuilder,
          $$GetBetterEventOutboxTableUpdateCompanionBuilder,
          (
            GetBetterEventOutboxData,
            BaseReferences<
              _$AppDatabase,
              $GetBetterEventOutboxTable,
              GetBetterEventOutboxData
            >,
          ),
          GetBetterEventOutboxData,
          PrefetchHooks Function()
        > {
  $$GetBetterEventOutboxTableTableManager(
    _$AppDatabase db,
    $GetBetterEventOutboxTable table,
  ) : super(
        TableManagerState(
          db: db,
          table: table,
          createFilteringComposer: () =>
              $$GetBetterEventOutboxTableFilterComposer($db: db, $table: table),
          createOrderingComposer: () =>
              $$GetBetterEventOutboxTableOrderingComposer(
                $db: db,
                $table: table,
              ),
          createComputedFieldComposer: () =>
              $$GetBetterEventOutboxTableAnnotationComposer(
                $db: db,
                $table: table,
              ),
          updateCompanionCallback:
              ({
                Value<String> eventId = const Value.absent(),
                Value<String> userId = const Value.absent(),
                Value<String> eventType = const Value.absent(),
                Value<String> storyId = const Value.absent(),
                Value<int> storyVersion = const Value.absent(),
                Value<DateTime> occurredAt = const Value.absent(),
                Value<int> rowid = const Value.absent(),
              }) => GetBetterEventOutboxCompanion(
                eventId: eventId,
                userId: userId,
                eventType: eventType,
                storyId: storyId,
                storyVersion: storyVersion,
                occurredAt: occurredAt,
                rowid: rowid,
              ),
          createCompanionCallback:
              ({
                required String eventId,
                required String userId,
                required String eventType,
                required String storyId,
                required int storyVersion,
                required DateTime occurredAt,
                Value<int> rowid = const Value.absent(),
              }) => GetBetterEventOutboxCompanion.insert(
                eventId: eventId,
                userId: userId,
                eventType: eventType,
                storyId: storyId,
                storyVersion: storyVersion,
                occurredAt: occurredAt,
                rowid: rowid,
              ),
          withReferenceMapper: (p0) => p0
              .map((e) => (e.readTable(table), BaseReferences(db, table, e)))
              .toList(),
          prefetchHooksCallback: null,
        ),
      );
}

typedef $$GetBetterEventOutboxTableProcessedTableManager =
    ProcessedTableManager<
      _$AppDatabase,
      $GetBetterEventOutboxTable,
      GetBetterEventOutboxData,
      $$GetBetterEventOutboxTableFilterComposer,
      $$GetBetterEventOutboxTableOrderingComposer,
      $$GetBetterEventOutboxTableAnnotationComposer,
      $$GetBetterEventOutboxTableCreateCompanionBuilder,
      $$GetBetterEventOutboxTableUpdateCompanionBuilder,
      (
        GetBetterEventOutboxData,
        BaseReferences<
          _$AppDatabase,
          $GetBetterEventOutboxTable,
          GetBetterEventOutboxData
        >,
      ),
      GetBetterEventOutboxData,
      PrefetchHooks Function()
    >;

class $AppDatabaseManager {
  final _$AppDatabase _db;
  $AppDatabaseManager(this._db);
  $$ChatSessionsTableTableManager get chatSessions =>
      $$ChatSessionsTableTableManager(_db, _db.chatSessions);
  $$ChatMessagesTableTableManager get chatMessages =>
      $$ChatMessagesTableTableManager(_db, _db.chatMessages);
  $$ChatSyncJobsTableTableManager get chatSyncJobs =>
      $$ChatSyncJobsTableTableManager(_db, _db.chatSyncJobs);
  $$GetBetterCatalogCachesTableTableManager get getBetterCatalogCaches =>
      $$GetBetterCatalogCachesTableTableManager(
        _db,
        _db.getBetterCatalogCaches,
      );
  $$GetBetterStoryProgressTableTableManager get getBetterStoryProgress =>
      $$GetBetterStoryProgressTableTableManager(
        _db,
        _db.getBetterStoryProgress,
      );
  $$GetBetterEventOutboxTableTableManager get getBetterEventOutbox =>
      $$GetBetterEventOutboxTableTableManager(_db, _db.getBetterEventOutbox);
}
