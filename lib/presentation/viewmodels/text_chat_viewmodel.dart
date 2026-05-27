import 'chat_viewmodel.dart';

/// ViewModel for the main Buddy text-chat screen opened from the drawer.
/// On a normal app open, delegates to the base session lifecycle (reuse empty
/// or create fresh). Overrides only for the FCM engagement tap path where a
/// specific session ID is pre-selected.
class TextChatViewModel extends ChatViewModel {
  final String? initialSessionId;

  TextChatViewModel({
    this.initialSessionId,
    required super.backendService,
    required super.connectivityService,
    required super.chatRepository,
    required super.chatBackupService,
    required super.feedbackService,
    required super.chatSessionManager,
    required super.postHogAnalyticsService,
  });

  @override
  String? get agentId => null;

  @override
  Future<void> initializeSession() async {
    if (initialSessionId != null) {
      await switchSession(initialSessionId!);
    } else {
      await super.initializeSession();
    }
  }
}
