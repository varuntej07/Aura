import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../../viewmodels/auth_viewmodel.dart';
import '../../viewmodels/text_chat_viewmodel.dart';
import '../../viewmodels/view_state.dart';
import '../../widgets/chat_message_list.dart';
import '../../widgets/error_display.dart';
import '../../widgets/message_input.dart';
import '../reminders/reminders_screen.dart';

class EmbeddedChatPanel extends StatefulWidget {
  const EmbeddedChatPanel({super.key});

  @override
  State<EmbeddedChatPanel> createState() => _EmbeddedChatPanelState();
}

class _EmbeddedChatPanelState extends State<EmbeddedChatPanel>
    with AutomaticKeepAliveClientMixin {
  final _scrollController = ScrollController();

  @override
  bool get wantKeepAlive => true;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addPostFrameCallback((_) async {
      final uid = context.read<AuthViewModel>().user?.uid;
      await context.read<TextChatViewModel>().init(uid);
      _jumpToBottom();
    });
  }

  @override
  void dispose() {
    _scrollController.dispose();
    super.dispose();
  }

  void _jumpToBottom() {
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (_scrollController.hasClients) {
        _scrollController.jumpTo(_scrollController.position.maxScrollExtent);
      }
    });
  }

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
    super.build(context);

    return Consumer<TextChatViewModel>(
      builder: (context, vm, _) {
        if (vm.isStreaming) {
          WidgetsBinding.instance
              .addPostFrameCallback((_) => _scrollToBottom());
        }

        return Column(
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
                        RemindersScreen.route(context),
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
            // Floating nav bar spacer: full height when keyboard is hidden,
            // zero when keyboard is open (Scaffold already shrinks the body).
            MessageInput(
              isLoading: vm.state == ViewState.loading,
              hint: 'Ask Buddy anything...',
              onSend: (text) {
                vm.sendMessage(text, _uid);
                _scrollToBottom();
              },
              onStop: vm.stopGeneration,
            ),
            SizedBox(
              height: MediaQuery.viewInsetsOf(context).bottom > 0
                  ? 0
                  : MediaQuery.viewPaddingOf(context).bottom + 96,
            ),
          ],
        );
      },
    );
  }
}
