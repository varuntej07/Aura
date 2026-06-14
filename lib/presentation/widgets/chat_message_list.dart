import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import '../../core/theme/app_colors.dart';
import '../../data/models/chat_message_model.dart';
import '../../data/models/streaming_snapshot.dart';
import 'clarification_card.dart';
import 'buddy_response_bubble.dart';
import 'streaming_message_bubble.dart';

typedef OnRetry = void Function(String messageId);
typedef OnEdit = void Function(String messageId, String newText);
typedef OnFeedback = void Function(String messageId, MessageFeedback? feedback);

/// Scrollable message list shared by all chat screens.
/// Wrapped in [RepaintBoundary] so streaming delta updates only repaint
/// this subtree, not the enclosing screen.
class ChatMessageList extends StatelessWidget {
  final List<ChatMessageModel> messages;
  final bool isStreaming;
  /// Live streaming output. The streaming bubble binds to this so a token
  /// repaints only that bubble — the rest of the list stays put.
  final ValueListenable<StreamingSnapshot>? streamingOutput;
  final ScrollController scrollController;
  final OnRetry? onRetry;
  final OnEdit? onEdit;
  final OnFeedback? onFeedback;
  final VoidCallback? onViewReminders;
  final void Function(String clarificationId, List<String> options)? onClarificationSubmit;

  const ChatMessageList({
    super.key,
    required this.messages,
    required this.scrollController,
    this.isStreaming = false,
    this.streamingOutput,
    this.onRetry,
    this.onEdit,
    this.onFeedback,
    this.onViewReminders,
    this.onClarificationSubmit,
  });

  @override
  Widget build(BuildContext context) {
    int lastAssistantIdx = -1;
    for (var i = messages.length - 1; i >= 0; i--) {
      if (!messages[i].isUser) {
        lastAssistantIdx = i;
        break;
      }
    }

    final totalItems = messages.length + (isStreaming ? 1 : 0);

    return RepaintBoundary(
      // One SelectionArea for the whole list keeps text selectable (even across
      // bubbles) without paying the per-bubble `selectable` cost on every build.
      child: SelectionArea(
        child: ListView.builder(
          controller: scrollController,
          padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 8),
          itemCount: totalItems,
          itemBuilder: (context, index) {
            if (index < messages.length) {
              final msg = messages[index];

              if (msg.clarificationPayload != null) {
                return ClarificationCard(
                  payload: msg.clarificationPayload!,
                  onSubmit: (options) => onClarificationSubmit?.call(
                    msg.clarificationPayload!.clarificationId,
                    options,
                  ),
                );
              }

              return BuddyResponseBubble(
                message: msg,
                isLastAssistantMessage: index == lastAssistantIdx,
                onRetry: onRetry,
                onEdit: onEdit,
                onFeedback: onFeedback,
                onViewReminders: onViewReminders,
              );
            }

            // Streaming slot - binds to the live notifier so a token repaints
            // only this bubble (not the list), and keeps it pinned to the bottom.
            final output = streamingOutput;
            if (output == null) return const SizedBox.shrink();
            return ValueListenableBuilder<StreamingSnapshot>(
              valueListenable: output,
              builder: (context, snapshot, _) {
                WidgetsBinding.instance.addPostFrameCallback((_) {
                  if (scrollController.hasClients) {
                    scrollController
                        .jumpTo(scrollController.position.maxScrollExtent);
                  }
                });
                return StreamingMessageBubble(
                  streamingText: snapshot.text,
                  thinkingMessage: snapshot.thinkingMessage,
                  isLoading: true,
                );
              },
            );
          },
        ),
      ),
    );
  }
}

/// Shown when a chat thread has no messages yet.
///
/// If [initialMessage] is provided (e.g. from a notification tap), it is
/// shown as a speech bubble from the agent to prime the conversation topic.
class EmptyChatPlaceholder extends StatelessWidget {
  final String agentName;
  final String? initialMessage;

  const EmptyChatPlaceholder({
    super.key,
    required this.agentName,
    this.initialMessage,
  });

  @override
  Widget build(BuildContext context) {
    final opener = initialMessage;
    if (opener != null && opener.isNotEmpty) {
      return Padding(
        padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 24),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          mainAxisSize: MainAxisSize.min,
          children: [
            Container(
              padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 10),
              decoration: BoxDecoration(
                color: AppColors.surface,
                borderRadius: BorderRadius.circular(16),
              ),
              child: Text(
                opener,
                style: const TextStyle(
                  color: AppColors.textPrimary,
                  fontSize: 14,
                  height: 1.45,
                ),
              ),
            ),
          ],
        ),
      );
    }

    return Center(
      child: Text(
        'Start a conversation with $agentName',
        style: const TextStyle(color: AppColors.textTertiary, fontSize: 14),
        textAlign: TextAlign.center,
      ),
    );
  }
}
