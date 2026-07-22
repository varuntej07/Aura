import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';
import 'package:provider/provider.dart';

import '../../../core/theme/app_colors.dart';
import '../../../core/theme/glass_card.dart';
import '../../../data/services/backend_api_service.dart';
import '../../viewmodels/auth_viewmodel.dart';
import '../../viewmodels/notification_chat_seed.dart';
import '../../viewmodels/text_chat_viewmodel.dart';
import '../../widgets/chat_history_drawer.dart';
import '../../widgets/chat_message_list.dart';
import '../../widgets/chat_suggestion_pills.dart';
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
  final _inputController = TextEditingController();
  final _inputFocusNode = FocusNode();

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addPostFrameCallback((_) async {
      final uid = context.read<AuthViewModel>().user?.uid;
      final chatVm = context.read<TextChatViewModel>();
      final extra = GoRouterState.of(context).extra;
      await chatVm.init(uid);
      _jumpToBottom();

      // A proactive notification tap arrives as one typed NotificationChatSeed
      // (built in home_screen). Switching on the origin is exhaustive, so a new
      // decider that forgets a branch is a compile warning, not an empty chat.
      if (!mounted) return;
      
      if (extra is NotificationChatSeed) {
        switch (extra.origin) {
          case NotificationChatOrigin.engagement:
            await chatVm.loadEngagementContext(
              engagementId: extra.engagementId,
              agentContext: extra.agentContext,
              initialMessage: extra.openingMessage,
            );
          case NotificationChatOrigin.signal:
            // Signal-engine content notification: seed the opener and arm funnel
            // attribution for the user's first reply.
            await chatVm.loadSignalNotificationContext(
              notificationId: extra.notificationId,
              contentId: extra.contentId,
              category: extra.category,
              initialMessage: extra.openingMessage,
              notificationReason: extra.notificationReason,
            );
          case NotificationChatOrigin.thread:
            // Curiosity follow-up: reconcile any shade exchange from the server,
            // otherwise seed Buddy's question + the suggestion pills.
            final prior = await context.read<BackendApiService>().fetchThreadMessages(extra.threadId);
            
            if (!mounted) return;
            
            await chatVm.loadThreadFollowUpContext(
              threadId: extra.threadId,
              question: extra.openingMessage,
              suggestedReplies: extra.suggestedReplies,
              priorMessages: prior,
              notificationReason: extra.notificationReason,
            );
          case NotificationChatOrigin.icebreaker:
            // Icebreaker opener: seed Buddy's opener and arm the icebreaker funnel.
            await chatVm.loadIcebreakerContext(
              notificationId: extra.notificationId,
              openingMessage: extra.openingMessage,
              notificationReason: extra.notificationReason,
            );
          case NotificationChatOrigin.tracker:
            // Topic-tracker live update: seed Buddy's update as the opener.
            await chatVm.loadTrackerContext(
              openingMessage: extra.openingMessage,
            );
        }
        _jumpToBottom();
      }
    });
  }

  @override
  void dispose() {
    _scrollController.dispose();
    _inputController.dispose();
    _inputFocusNode.dispose();
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

  /// Drops a tapped starter into the input box and focuses it, so the user can
  /// edit before sending — the pills are conversation openers, not auto-sends.
  void _fillInput(String text) {
    if (context.read<AuthViewModel>().user == null) {
      showSignInGateDialog(context, authViewModel: context.read<AuthViewModel>());
      return;
    }
    _inputController.text = text;
    _inputController.selection =
        TextSelection.collapsed(offset: text.length);
    _inputFocusNode.requestFocus();
  }

  @override
  Widget build(BuildContext context) {
    return Consumer<TextChatViewModel>(
      builder: (context, vm, _) {
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
                      ? (vm.threadSuggestions.isEmpty &&
                              vm.suggestionPills.isNotEmpty
                          // Personalized starters, centered. Tapping fills the
                          // input box (see [_fillInput]) instead of auto-sending.
                          ? ChatSuggestionPills(
                              pills: vm.suggestionPills,
                              onTap: _fillInput,
                            )
                          : const EmptyChatPlaceholder(agentName: 'Buddy'))
                      : ChatMessageList(
                          messages: vm.messages,
                          scrollController: _scrollController,
                          isStreaming: vm.isStreaming,
                          streamingOutput: vm.streamingOutput,
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
                if (vm.threadSuggestions.isNotEmpty)
                  _ThreadSuggestionPills(
                    suggestions: vm.threadSuggestions,
                    onTap: (text) {
                      if (context.read<AuthViewModel>().user == null) {
                        showSignInGateDialog(context, authViewModel: context.read<AuthViewModel>());
                        return;
                      }
                      vm.sendMessage(text, _uid);
                      _scrollToBottom();
                    },
                  ),
                MessageInput(
                  controller: _inputController,
                  focusNode: _inputFocusNode,
                  isLoading: vm.state == ViewState.loading,
                  hint: 'Ask Buddy anything…',
                  allowAttachments: context.read<AuthViewModel>().user != null,
                  onSend: (text, attachments, inputMethod) {
                    if (context.read<AuthViewModel>().user == null) {
                      showSignInGateDialog(context, authViewModel: context.read<AuthViewModel>());
                      return;
                    }
                    vm.sendMessage(
                      text,
                      _uid,
                      attachments: attachments,
                      inputMethod: inputMethod,
                    );
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

/// Suggestion chips for a curiosity follow-up, shown above the input until the
/// user replies. Presentational: data + callback arrive via the constructor.
class _ThreadSuggestionPills extends StatelessWidget {
  const _ThreadSuggestionPills({required this.suggestions, required this.onTap});

  final List<String> suggestions;
  final ValueChanged<String> onTap;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.fromLTRB(12, 4, 12, 8),
      child: Wrap(
        spacing: 8,
        runSpacing: 8,
        children: [
          for (final reply in suggestions)
            GestureDetector(
              onTap: () => onTap(reply),
              child: FauxGlassCard.pill(
                child: Text(
                  reply,
                  style: const TextStyle(color: AppColors.textSecondary, fontSize: 13),
                ),
              ),
            ),
        ],
      ),
    );
  }
}
