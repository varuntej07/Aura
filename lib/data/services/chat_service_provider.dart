import '../models/chat_attachment.dart';
import 'backend_api_service.dart';

/// Abstract interface for chat/AI streaming access.
///
/// [BackendApiService] (production) and [StubChatServiceProvider] (dev) both
/// implement this. Selection happens at DI time in lib/di/providers.dart.
/// [ChatViewModel] and its subclasses depend only on this interface, never on [BackendApiService] directly.
abstract class ChatServiceProvider {
  /// Streams a chat response as a sequence of [ChatStreamEvent] objects.
  /// The stream completes after a [DoneEvent] or [ErrorStreamEvent].
  Stream<ChatStreamEvent> sendMessageStream(
    String message,
    String userId, {
    List<Map<String, dynamic>> history = const [],
    String? sessionId,
    String? clientMessageId,
    String? agentId,
    List<ChatAttachment>? attachments,
    String? notificationReason,
  });

  /// Fire-and-forget: marks an engagement notification as responded.
  /// Failures are swallowed and never thrown
  Future<void> markEngagementResponded(String engagementId);
}
