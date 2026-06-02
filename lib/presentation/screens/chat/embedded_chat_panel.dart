import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';
import 'package:provider/provider.dart';

import '../../../core/theme/app_colors.dart';
import '../../../core/theme/glass_card.dart';
import '../../viewmodels/auth_viewmodel.dart';
import '../../viewmodels/text_chat_viewmodel.dart';
import '../../widgets/chat_message_list.dart';
import '../../widgets/error_display.dart';
import '../../widgets/message_input.dart';
import '../../widgets/sign_in_gate_dialog.dart';
import '../reminders/reminders_screen.dart';

class EmbeddedChatPanel extends StatefulWidget {
  const EmbeddedChatPanel({super.key});

  @override
  State<EmbeddedChatPanel> createState() => _EmbeddedChatPanelState();
}

class _EmbeddedChatPanelState extends State<EmbeddedChatPanel>
    with AutomaticKeepAliveClientMixin, WidgetsBindingObserver {
  final _scrollController = ScrollController();
  double _keyboardHeight = 0;
  AuthViewModel? _authVm;
  VoidCallback? _authListener;

  @override
  bool get wantKeepAlive => true;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    WidgetsBinding.instance.addPostFrameCallback((_) async {
      if (!mounted) return;
      final authVm = context.read<AuthViewModel>();
      final vm = context.read<TextChatViewModel>();
      await vm.init(authVm.user?.uid);
      _jumpToBottom();
      // Re-run restore once auth resolves — covers init firing before sign-in
      // finished on a fresh install (the window where history could look gone).
      _authVm = authVm;
      _authListener = () => vm.ensureRestoredForUser(authVm.user?.uid);
      authVm.addListener(_authListener!);
    });
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    if (_authListener != null) _authVm?.removeListener(_authListener!);
    _scrollController.dispose();
    super.dispose();
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state != AppLifecycleState.paused &&
        state != AppLifecycleState.resumed) {
      return;
    }
    if (!mounted) return;
    final uid = context.read<AuthViewModel>().user?.uid;
    if (uid != null && uid.isNotEmpty) {
      context.read<TextChatViewModel>().flushPendingBackup(uid);
    }
  }

  @override
  void didChangeMetrics() {
    final view = WidgetsBinding.instance.platformDispatcher.views.first;
    final newHeight = view.viewInsets.bottom / view.devicePixelRatio;
    final wasOpen = _keyboardHeight > 100;
    _keyboardHeight = newHeight;
    final isOpen = newHeight > 100;
    if (wasOpen != isOpen) setState(() {});
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

        if (vm.chatLimitReached) {
          WidgetsBinding.instance.addPostFrameCallback((_) {
            vm.clearChatLimitReached();
            context.push('/paywall');
          });
        }

        final authVm = context.read<AuthViewModel>();
        final showFirstSession =
            authVm.justCompletedOnboarding && vm.messages.isEmpty && !vm.isStreaming;

        return Column(
          children: [
            Expanded(
              child: vm.messages.isEmpty && !vm.isStreaming
                  ? showFirstSession
                      ? _FirstSessionPrompt(
                          onSuggestionTap: (text) {
                            authVm.consumeFirstSessionPrompt();
                            vm.sendMessage(text, _uid);
                            _scrollToBottom();
                          },
                        )
                      : const EmptyChatPlaceholder(agentName: 'Buddy')
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
              hint: 'Ask Buddy anything...',
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
            ),
            AnimatedContainer(
              duration: const Duration(milliseconds: 200),
              curve: Curves.easeOut,
              height: _keyboardHeight > 100
                  ? 0
                  : MediaQuery.viewPaddingOf(context).bottom + 99,
            ),
          ],
        );
      },
    );
  }
}

// First-session prompt — shown once immediately after onboarding.
// Tap a suggestion to seed the conversation (and the Aura profile) right away.

class _FirstSessionPrompt extends StatelessWidget {
  final ValueChanged<String> onSuggestionTap;

  static const _suggestions = [
    "Tell Buddy about me, my goals, routine, and what's on my mind",
    "I want Buddy to check in on me daily and remind me of my goals",
    "Help me stay on top of my habits and send me morning briefings",
    "I just moved to a new city and need a thinking partner",
  ];

  const _FirstSessionPrompt({required this.onSuggestionTap});

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 24),
      child: Column(
        mainAxisAlignment: MainAxisAlignment.center,
        children: [
          Container(
            width: 64,
            height: 64,
            decoration: BoxDecoration(
              color: AppColors.accent.withValues(alpha: 0.10),
              shape: BoxShape.circle,
            ),
            child: const Icon(
              Icons.auto_awesome_rounded,
              size: 28,
              color: AppColors.accent,
            ),
          ),
          const SizedBox(height: 20),
          const Text(
            'Hey, I\'m Buddy.',
            style: TextStyle(
              color: AppColors.textPrimary,
              fontSize: 22,
              fontWeight: FontWeight.w700,
              letterSpacing: -0.5,
            ),
          ),
          const SizedBox(height: 8),
          const Text(
            'Tell me about yourself so I can actually remember you.\nOr pick a starter below.',
            style: TextStyle(
              color: AppColors.textSecondary,
              fontSize: 14,
              height: 1.6,
            ),
            textAlign: TextAlign.center,
          ),
          const SizedBox(height: 28),
          ..._suggestions.map(
            (suggestion) => Padding(
              padding: const EdgeInsets.only(bottom: 10),
              child: GestureDetector(
                onTap: () => onSuggestionTap(suggestion),
                child: FauxGlassCard(
                  borderRadius: 14,
                  padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 13),
                  child: Row(
                    children: [
                      Expanded(
                        child: Text(
                          suggestion,
                          style: const TextStyle(
                            color: AppColors.textPrimary,
                            fontSize: 13,
                            height: 1.4,
                          ),
                        ),
                      ),
                      const SizedBox(width: 8),
                      const Icon(
                        Icons.arrow_forward_ios_rounded,
                        size: 12,
                        color: AppColors.textTertiary,
                      ),
                    ],
                  ),
                ),
              ),
            ),
          ),
        ],
      ),
    );
  }
}
