import 'dart:convert';
import 'dart:typed_data';

import '../services/backend_api_service.dart';
import 'chat_attachment.dart';
import 'clarification_payload.dart';

enum ChatMessageChannel { text, voice }

enum MessageFeedback { liked, disliked }

enum MessageStatus { sent, error }

class ChatMessageModel {
  final String id;
  final String text;
  final bool isUser;
  final DateTime timestamp;
  final ChatMessageChannel channel;
  final MessageStatus status;
  final MessageFeedback? feedback;
  final String? errorReason;

  /// Null until the message is persisted to a SQLite session.
  final String? sessionId;

  /// Set when this message was pre-inserted from an FCM engagement tap.
  final String? engagementId;
  final String? engagementAgent;

  /// Non-null when this assistant message was produced by a set_reminder call.
  /// Drives the inline ReminderCard widget in chat.
  final ReminderPayload? reminderPayload;

  /// Non-null when the assistant used ask_clarification. Drives the
  /// ClarificationCard widget; becomes read-only once selectedOptions is set.
  final ClarificationPayload? clarificationPayload;

  /// Holds the files the user attached when sending this message.
  /// Full bytes live in memory only during the session. After app restart,
  /// only metadata + thumbnails are rehydrated from the local database.
  final List<ChatAttachment>? attachments;

  const ChatMessageModel({
    required this.id,
    required this.text,
    required this.isUser,
    required this.timestamp,
    required this.channel,
    this.status = MessageStatus.sent,
    this.feedback,
    this.errorReason,
    this.sessionId,
    this.engagementId,
    this.engagementAgent,
    this.reminderPayload,
    this.clarificationPayload,
    this.attachments,
  });

  // ── Serialisation ─────────────────────────────────────────────────────────

  factory ChatMessageModel.fromMap(Map<String, dynamic> map) {
    return ChatMessageModel(
      id: map['id'] as String,
      text: map['text'] as String,
      isUser: map['is_user'] as bool,
      timestamp: DateTime.parse(map['timestamp'] as String),
      channel: ChatMessageChannel.values.firstWhere(
        (c) => c.name == map['channel'],
        orElse: () => ChatMessageChannel.text,
      ),
      status: MessageStatus.values.firstWhere(
        (s) => s.name == map['status'],
        orElse: () => MessageStatus.sent,
      ),
      feedback: map['feedback'] == null
          ? null
          : MessageFeedback.values.firstWhere(
              (f) => f.name == map['feedback'],
              orElse: () => MessageFeedback.liked,
            ),
      errorReason: map['error_reason'] as String?,
      sessionId: map['session_id'] as String?,
      engagementId: map['engagement_id'] as String?,
      engagementAgent: map['engagement_agent'] as String?,
      reminderPayload: ReminderPayload.tryFromJsonString(
        map['reminder_json'] as String?,
      ),
      clarificationPayload: ClarificationPayload.tryFromJsonString(
        map['clarification_json'] as String?,
      ),
      attachments: _attachmentsFromJson(map['attachment_json'] as String?),
    );
  }

  Map<String, dynamic> toMap() => {
        'id': id,
        'text': text,
        'is_user': isUser,
        'timestamp': timestamp.toUtc().toIso8601String(),
        'channel': channel.name,
        'status': status.name,
        if (feedback != null) 'feedback': feedback!.name,
        if (errorReason != null) 'error_reason': errorReason,
        if (sessionId != null) 'session_id': sessionId,
        if (engagementId != null) 'engagement_id': engagementId,
        if (engagementAgent != null) 'engagement_agent': engagementAgent,
        if (reminderPayload != null) 'reminder_json': reminderPayload!.toJsonString(),
        if (clarificationPayload != null)
          'clarification_json': clarificationPayload!.toJsonString(),
        if (attachments != null && attachments!.isNotEmpty)
          'attachment_json': _attachmentsToJson(attachments!),
      };

  /// Serialises to the `{role, content}` shape expected by the Claude /chat
  /// history parameter. When [includeAttachments] is true and this message has
  /// attachments with full bytes, content is a list of Anthropic content blocks.
  /// Otherwise content is a plain text string.
  Map<String, dynamic> toHistoryTurn({bool includeAttachments = false}) {
    final role = isUser ? 'user' : 'assistant';
    final textContent = clarificationPayload?.question ?? text;

    if (!includeAttachments || !isUser || attachments == null || attachments!.isEmpty) {
      return {'role': role, 'content': textContent};
    }

    final hasFullBytes = attachments!.any((a) => a.bytes.isNotEmpty);
    if (!hasFullBytes) {
      return {'role': role, 'content': textContent};
    }

    final blocks = <Map<String, dynamic>>[];
    for (final att in attachments!) {
      if (att.bytes.isEmpty) continue;
      if (att.type == ChatAttachmentType.image) {
        blocks.add({
          'type': 'image',
          'source': {'type': 'base64', 'media_type': att.mimeType, 'data': base64Encode(att.bytes)},
        });
      } else {
        blocks.add({
          'type': 'document',
          'source': {'type': 'base64', 'media_type': att.mimeType, 'data': base64Encode(att.bytes)},
        });
      }
    }
    if (textContent.isNotEmpty) {
      blocks.add({'type': 'text', 'text': textContent});
    }
    return {'role': role, 'content': blocks};
  }

  // ── Value equality ────────────────────────────────────────────────────────

  @override
  bool operator ==(Object other) =>
      identical(this, other) ||
      other is ChatMessageModel &&
          runtimeType == other.runtimeType &&
          id == other.id;

  @override
  int get hashCode => id.hashCode;

  // ── copyWith ──────────────────────────────────────────────────────────────

  ChatMessageModel copyWith({
    String? id,
    String? text,
    bool? isUser,
    DateTime? timestamp,
    ChatMessageChannel? channel,
    MessageStatus? status,
    MessageFeedback? Function()? feedback,
    String? Function()? errorReason,
    String? sessionId,
    String? engagementId,
    String? engagementAgent,
    ReminderPayload? Function()? reminderPayload,
    ClarificationPayload? Function()? clarificationPayload,
    List<ChatAttachment>? Function()? attachments,
  }) {
    return ChatMessageModel(
      id: id ?? this.id,
      text: text ?? this.text,
      isUser: isUser ?? this.isUser,
      timestamp: timestamp ?? this.timestamp,
      channel: channel ?? this.channel,
      status: status ?? this.status,
      feedback: feedback != null ? feedback() : this.feedback,
      errorReason: errorReason != null ? errorReason() : this.errorReason,
      sessionId: sessionId ?? this.sessionId,
      engagementId: engagementId ?? this.engagementId,
      engagementAgent: engagementAgent ?? this.engagementAgent,
      reminderPayload:
          reminderPayload != null ? reminderPayload() : this.reminderPayload,
      clarificationPayload: clarificationPayload != null
          ? clarificationPayload()
          : this.clarificationPayload,
      attachments: attachments != null ? attachments() : this.attachments,
    );
  }

  @override
  String toString() =>
      'ChatMessageModel(id: $id, isUser: $isUser, channel: ${channel.name}, status: ${status.name})';

  // ── Attachment JSON helpers ───────────────────────────────────────────────

  static String _attachmentsToJson(List<ChatAttachment> attachments) {
    final list = attachments.map((a) => {
          'fileName': a.fileName,
          'mimeType': a.mimeType,
          'type': a.type.name,
          if (a.thumbnail != null) 'thumbnail': base64Encode(a.thumbnail!),
        }).toList();
    return jsonEncode(list);
  }

  static List<ChatAttachment>? _attachmentsFromJson(String? json) {
    if (json == null || json.isEmpty) return null;
    try {
      final list = jsonDecode(json) as List;
      if (list.isEmpty) return null;
      return list.map((item) {
        final map = item as Map<String, dynamic>;
        final thumbBase64 = map['thumbnail'] as String?;
        return ChatAttachment(
          fileName: map['fileName'] as String,
          mimeType: map['mimeType'] as String,
          fileSizeBytes: 0,
          bytes: Uint8List(0),
          type: ChatAttachmentType.values.firstWhere(
            (t) => t.name == map['type'],
            orElse: () => ChatAttachmentType.document,
          ),
          thumbnail: thumbBase64 != null ? base64Decode(thumbBase64) : null,
        );
      }).toList();
    } catch (_) {
      return null;
    }
  }
}
