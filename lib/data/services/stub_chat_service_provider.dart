import '../models/chat_attachment.dart';
import 'backend_api_service.dart';
import 'chat_service_provider.dart';

/// Dev-only stub implementation of [ChatServiceProvider].
/// Used when [Environment.hasConfiguredApi] is false.
/// Simulates a streaming response without any network calls.
class StubChatServiceProvider implements ChatServiceProvider {
  @override
  Stream<ChatStreamEvent> sendMessageStream(
    String message,
    String userId, {
    List<Map<String, dynamic>> history = const [],
    String? sessionId,
    String? clientMessageId,
    String? agentId,
    List<ChatAttachment>? attachments,
  }) async* {
    await Future.delayed(const Duration(milliseconds: 600));
    const words = ['This ', 'is ', 'a ', 'stub ', 'response.'];
    for (final w in words) {
      yield TextDeltaEvent(w);
      await Future.delayed(const Duration(milliseconds: 120));
    }
    yield DoneEvent(metadata: const {'tool_names': []});
  }

  @override
  Future<void> markEngagementResponded(String engagementId) async {}
}
