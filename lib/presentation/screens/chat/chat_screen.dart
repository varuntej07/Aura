import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';
import 'package:provider/provider.dart';

import '../../../core/theme/app_colors.dart';
import '../../viewmodels/auth_viewmodel.dart';
import '../../viewmodels/text_chat_viewmodel.dart';
import '../../widgets/chat_history_drawer.dart';
import '../../widgets/chat_message_list.dart';
import '../../widgets/error_display.dart';
import '../../widgets/message_input.dart';
import '../../widgets/sign_in_gate_dialog.dart';
import '../reminders/reminders_screen.dart';

/// Full-screen Buddy text chat. Opened from the home drawer.
/// Scoped [TextChatViewModel] is provided by the router.
class ChatScreen extends StatefulWidget {
  const ChatScreen({super.key});

  @override
  State<ChatScreen> createState() => _ChatScreenState();
}

class _ChatScreenState extends State<ChatScreen> {
  final _scaffoldKey = GlobalKey<ScaffoldState>();
  final _scrollController = ScrollController();

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addPostFrameCallback((_) async {
      final uid = context.read<AuthViewModel>().user?.uid;
      final chatVm = context.read<TextChatViewModel>();
      final extra = GoRouterState.of(context).extra;
      await chatVm.init(uid);
      _jumpToBottom();

      // Handle engagement pre-load passed as route extra
      if (!mounted) return;
      if (extra is Map<String, dynamic> &&
          extra.containsKey('engagementId')) {
        await chatVm.loadEngagementContext(
              engagementId: extra['engagementId'] as String,
              agentContext: extra['agentContext'] as String,
              initialMessage: extra['initialMessage'] as String,
            );
        _jumpToBottom();
      } else if (extra is Map<String, dynamic> &&
          extra.containsKey('signalNotificationId')) {
        // Signal-engine content notification: seed the opener and arm funnel
        // attribution for the user's first reply.
        await chatVm.loadSignalNotificationContext(
              notificationId: extra['signalNotificationId'] as String,
              contentId: extra['signalContentId'] as String? ?? '',
              category: extra['signalCategory'] as String? ?? '',
              initialMessage: extra['initialMessage'] as String? ?? '',
            );
        _jumpToBottom();
      }
    });
  }

  @override
  void dispose() {
    _scrollController.dispose();
    super.dispose();
  }

  // Used on initial load and session switch — no animation so the user lands
  // directly at the last message without seeing the list scroll up from the top.
  void _jumpToBottom() {
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (_scrollController.hasClients) {
        _scrollController.jumpTo(_scrollController.position.maxScrollExtent);
      }
    });
  }

  // Used while streaming and after sending — animated so new content feels live.
  void _scrollToBottom() {
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (_scrollController.hasClients) {
        _scrollController.animateTo(
          _scrollController.position.maxScrollExtent,
          duration: const Duration(milliseconds: 250),
          curve: Curves.easeOut,
        );
      }
    });
  }

  String get _uid => context.read<AuthViewModel>().user?.uid ?? 'anonymous';

  @override
  Widget build(BuildContext context) {
    return Consumer<TextChatViewModel>(
      builder: (context, vm, _) {
        if (vm.isStreaming) {
          WidgetsBinding.instance
              .addPostFrameCallback((_) => _scrollToBottom());
        }

        return Scaffold(
          key: _scaffoldKey,
          backgroundColor: AppColors.deepBackground,
          resizeToAvoidBottomInset: true,
          appBar: AppBar(
            backgroundColor: AppColors.surface,
            elevation: 0,
            leading: IconButton(
              icon: const Icon(Icons.menu_rounded,
                  color: AppColors.textSecondary, size: 22),
              onPressed: () => _scaffoldKey.currentState?.openDrawer(),
            ),
            title: const Text(
              'Buddy',
              style: TextStyle(
                color: AppColors.textPrimary,
                fontSize: 16,
                fontWeight: FontWeight.w600,
              ),
            ),
            actions: [
              IconButton(
                icon: const Icon(Icons.arrow_back_ios_new_rounded,
                    color: AppColors.textPrimary, size: 20),
                onPressed: context.pop,
              ),
            ],
          ),
          drawer: ChatHistoryDrawer(
            sessions: vm.sessions,
            currentSessionId: vm.currentSessionId,
            onSessionSelected: (sessionId) {
              vm.switchSession(sessionId).then((_) => _jumpToBottom());
            },
            onNewChat: () {
              vm.startNewChat();
            },
          ),
          body: SafeArea(
            child: Column(
              children: [
                Expanded(
                  child: vm.messages.isEmpty && !vm.isStreaming
                      ? const EmptyChatPlaceholder(agentName: 'Buddy')
                      : ChatMessageList(
                          messages: vm.messages,
                          scrollController: _scrollController,
                          isStreaming: vm.isStreaming,
                          streamingText: vm.streamingText,
                          thinkingMessage: vm.thinkingMessage,
                          onRetry: vm.retryLastMessage,
                          onEdit: vm.editAndResend,
                          onFeedback: vm.setFeedback,
                          onViewReminders: () => Navigator.push(
                            context,
                            RemindersScreen.route(),
                          ),
                          onClarificationSubmit: vm.submitClarification,
                        ),
                ),
                if (vm.error != null)
                  Padding(
                    padding:
                        const EdgeInsets.symmetric(horizontal: 16, vertical: 4),
                    child: ErrorDisplay(
                      error: vm.error!,
                      onDismiss: vm.clearError,
                    ),
                  ),
                MessageInput(
                  isLoading: vm.state == ViewState.loading,
                  hint: 'Ask Buddy anything…',
                  allowAttachments: context.read<AuthViewModel>().user != null,
                  onSend: (text, attachments) {
                    if (context.read<AuthViewModel>().user == null) {
                      showSignInGateDialog(context);
                      return;
                    }
                    vm.sendMessage(text, _uid, attachments: attachments);
                    _scrollToBottom();
                  },
                  onStop: vm.stopGeneration,
                  extraBottomPadding: 16,
                ),
              ],
            ),
          ),
        );
      },
    );
  }
}
