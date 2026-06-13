import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_markdown_plus/flutter_markdown_plus.dart';
import 'package:url_launcher/url_launcher.dart';

import '../../core/theme/app_colors.dart';
import '../../core/theme/glass_card.dart';
import '../../data/models/chat_attachment.dart';
import '../../data/models/chat_message_model.dart';
import 'flash_alert.dart';
import 'full_screen_image_viewer.dart';
import 'reminder_card.dart';

/// Callback signatures for bubble actions.
typedef OnRetry = void Function(String messageId);
typedef OnEdit = void Function(String messageId, String newText);
typedef OnFeedback = void Function(String messageId, MessageFeedback? feedback);

class BuddyResponseBubble extends StatefulWidget {
  final ChatMessageModel message;
  final bool isLastAssistantMessage;
  final OnRetry? onRetry;
  final OnEdit? onEdit;
  final OnFeedback? onFeedback;

  /// Called when the user taps "View Reminders" on the inline ReminderCard.
  final VoidCallback? onViewReminders;

  const BuddyResponseBubble({
    super.key,
    required this.message,
    this.isLastAssistantMessage = false,
    this.onRetry,
    this.onEdit,
    this.onFeedback,
    this.onViewReminders,
  });

  @override
  State<BuddyResponseBubble> createState() => _BuddyResponseBubbleState();
}

class _BuddyResponseBubbleState extends State<BuddyResponseBubble> {
  bool _isEditing = false;
  late TextEditingController _editController;

  @override
  void initState() {
    super.initState();
    _editController = TextEditingController(text: widget.message.text);
  }

  @override
  void didUpdateWidget(covariant BuddyResponseBubble oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.message.id != widget.message.id) {
      _editController.text = widget.message.text;
      _isEditing = false;
    }
  }

  @override
  void dispose() {
    _editController.dispose();
    super.dispose();
  }

  void _copyToClipboard() {
    Clipboard.setData(ClipboardData(text: widget.message.text));
    if (mounted) {
      showFlashAlert(context, 'Copied to clipboard');
    }
  }

  void _startEditing() {
    setState(() {
      _isEditing = true;
      _editController.text = widget.message.text;
    });
  }

  void _cancelEditing() {
    setState(() => _isEditing = false);
  }

  void _submitEdit() {
    final newText = _editController.text.trim();
    if (newText.isEmpty || newText == widget.message.text) {
      _cancelEditing();
      return;
    }
    setState(() => _isEditing = false);
    widget.onEdit?.call(widget.message.id, newText);
  }

  @override
  Widget build(BuildContext context) {
    final msg = widget.message;
    final isUser = msg.isUser;
    final isError = msg.status == MessageStatus.error;

    return Align(
      alignment: isUser ? Alignment.centerRight : Alignment.centerLeft,
      child: Container(
        constraints: BoxConstraints(
          maxWidth: MediaQuery.of(context).size.width * 0.82,
        ),
        margin: const EdgeInsets.symmetric(vertical: 4),
        child: Column(
          crossAxisAlignment:
              isUser ? CrossAxisAlignment.end : CrossAxisAlignment.start,
          children: [
            // Message content 
            GestureDetector(
              onLongPress: isUser ? _startEditing : null,
              child: FauxGlassCard(
                padding:
                    const EdgeInsets.symmetric(horizontal: 16, vertical: 12),
                borderRadius: 18,
                borderColor: isUser
                    ? AppColors.accent.withValues(alpha: 0.30)
                    : isError
                        ? AppColors.error.withValues(alpha: 0.25)
                        : AppColors.glassBorderDim,
                gradient: isUser
                    ? LinearGradient(
                        begin: Alignment.topLeft,
                        end: Alignment.bottomRight,
                        colors: [
                          AppColors.accent.withValues(alpha: 0.20),
                          AppColors.accent.withValues(alpha: 0.08),
                        ],
                      )
                    : isError
                        ? LinearGradient(
                            begin: Alignment.topLeft,
                            end: Alignment.bottomRight,
                            colors: [
                              AppColors.error.withValues(alpha: 0.12),
                              AppColors.error.withValues(alpha: 0.05),
                            ],
                          )
                        : null,
                child: _isEditing
                    ? _buildEditField()
                    : isUser
                        ? _buildUserContent(msg)
                        : _buildAssistantContent(msg, isError),
              ),
            ),

            // ── Action row ──────────────────────────────────────────────
            if (!_isEditing) _buildActionRow(msg, isUser, isError),
          ],
        ),
      ),
    );
  }

  // User message: optional attachment previews + selectable text

  Widget _buildUserContent(ChatMessageModel msg) {
    final attachments = msg.attachments;
    final textWidget = SelectableText(
      msg.text,
      style: const TextStyle(
        color: AppColors.textPrimary,
        fontSize: 15,
        height: 1.5,
      ),
    );

    if (attachments == null || attachments.isEmpty) return textWidget;

    return Column(
      crossAxisAlignment: CrossAxisAlignment.end,
      mainAxisSize: MainAxisSize.min,
      children: [
        _AttachmentPreviewRow(attachments: attachments),
        const SizedBox(height: 8),
        textWidget,
      ],
    );
  }

  // Assistant message: markdown or error 

  Widget _buildAssistantContent(ChatMessageModel msg, bool isError) {
    if (isError) {
      return Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          const Icon(
            Icons.warning_amber_outlined,
            color: AppColors.warning,
            size: 18,
          ),
          const SizedBox(width: 8),
          Expanded(
            child: Text(
              msg.errorReason ?? 'Something went wrong. Tap retry to try again.',
              style: const TextStyle(
                color: AppColors.textSecondary,
                fontSize: 14,
                height: 1.5,
              ),
            ),
          ),
        ],
      );
    }

    final body = MarkdownBody(
      data: msg.text,
      selectable: true,
      onTapLink: (text, href, title) {
        if (href != null) {
          launchUrl(Uri.parse(href), mode: LaunchMode.externalApplication);
        }
      },
      styleSheet: _markdownStyleSheet(),
    );

    if (msg.reminderPayload == null) return body;

    // Reminder chip is embedded inside the bubble — feels like part of the
    // response rather than a separate system card.
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      mainAxisSize: MainAxisSize.min,
      children: [
        body,
        const SizedBox(height: 10),
        ReminderCard(
          reminder: msg.reminderPayload!,
          onViewReminders: widget.onViewReminders ?? () {},
        ),
      ],
    );
  }

  //  Inline edit field

  Widget _buildEditField() {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        TextField(
          controller: _editController,
          autofocus: true,
          maxLines: null,
          style: const TextStyle(
            color: AppColors.textPrimary,
            fontSize: 15,
            height: 1.5,
          ),
          decoration: const InputDecoration(
            border: InputBorder.none,
            focusedBorder: InputBorder.none,
            enabledBorder: InputBorder.none,
            errorBorder: InputBorder.none,
            disabledBorder: InputBorder.none,
            filled: false,
            contentPadding: EdgeInsets.zero,
            isDense: true,
          ),
        ),
        const SizedBox(height: 8),
        Row(
          mainAxisAlignment: MainAxisAlignment.end,
          children: [
            _ActionIcon(
              icon: Icons.close_rounded,
              onTap: _cancelEditing,
              tooltip: 'Cancel',
            ),
            const SizedBox(width: 12),
            _ActionIcon(
              icon: Icons.check_rounded,
              onTap: _submitEdit,
              tooltip: 'Save & resend',
              color: AppColors.accent,
            ),
          ],
        ),
      ],
    );
  }

  //  Action row (icons below the bubble)

  Widget _buildActionRow(ChatMessageModel msg, bool isUser, bool isError) {
    final actions = <Widget>[];

    if (isUser) return const SizedBox.shrink();

    actions.add(_ActionIcon(
      icon: Icons.content_copy_outlined,
      onTap: _copyToClipboard,
      tooltip: 'Copy',
    ));

    // Retry only on error responses
    if (isError && widget.onRetry != null) {
      actions.add(_ActionIcon(
        icon: Icons.refresh_outlined,
        onTap: () => widget.onRetry!(msg.id),
        tooltip: 'Retry',
      ));
    }

    // Feedback only on successful responses
    if (!isError && widget.onFeedback != null) {
      final isLiked = msg.feedback == MessageFeedback.liked;
      final isDisliked = msg.feedback == MessageFeedback.disliked;

      actions.add(_ActionIcon(
        icon: isLiked ? Icons.thumb_up : Icons.thumb_up_outlined,
        onTap: () => widget.onFeedback!(
          msg.id,
          isLiked ? null : MessageFeedback.liked,
        ),
        tooltip: 'Like',
        color: isLiked ? AppColors.accent : null,
      ));
      actions.add(_ActionIcon(
        icon: isDisliked ? Icons.thumb_down : Icons.thumb_down_outlined,
        onTap: () => widget.onFeedback!(
          msg.id,
          isDisliked ? null : MessageFeedback.disliked,
        ),
        tooltip: 'Dislike',
        color: isDisliked ? AppColors.error : null,
      ));
    }
    if (actions.isEmpty) return const SizedBox.shrink();

    return Padding(
      padding: const EdgeInsets.only(top: 4, bottom: 2),
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: actions
            .expand((w) => [w, const SizedBox(width: 4)])
            .toList()
          ..removeLast(),
      ),
    );
  }

  //  Markdown theme

  MarkdownStyleSheet _markdownStyleSheet() {
    return MarkdownStyleSheet(
      p: const TextStyle(
        color: AppColors.textPrimary,
        fontSize: 15,
        height: 1.5,
      ),
      h1: const TextStyle(
        color: AppColors.textPrimary,
        fontSize: 22,
        fontWeight: FontWeight.w700,
        height: 1.4,
      ),
      h2: const TextStyle(
        color: AppColors.textPrimary,
        fontSize: 19,
        fontWeight: FontWeight.w600,
        height: 1.4,
      ),
      h3: const TextStyle(
        color: AppColors.textPrimary,
        fontSize: 17,
        fontWeight: FontWeight.w600,
        height: 1.4,
      ),
      strong: const TextStyle(
        color: AppColors.textPrimary,
        fontWeight: FontWeight.w600,
      ),
      em: const TextStyle(
        color: AppColors.textSecondary,
        fontStyle: FontStyle.italic,
      ),
      code: TextStyle(
        color: AppColors.accentDark,
        backgroundColor: AppColors.surfaceVariant,
        fontSize: 13.5,
        fontFamily: 'monospace',
      ),
      codeblockDecoration: BoxDecoration(
        color: AppColors.surfaceVariant,
        borderRadius: BorderRadius.circular(8),
        border: Border.all(color: AppColors.border),
      ),
      codeblockPadding: const EdgeInsets.all(12),
      blockquoteDecoration: BoxDecoration(
        border: Border(
          left: BorderSide(color: AppColors.accent.withValues(alpha: 0.5), width: 3),
        ),
      ),
      blockquotePadding: const EdgeInsets.only(left: 12, top: 4, bottom: 4),
      listBullet: const TextStyle(color: AppColors.textSecondary),
      a: TextStyle(
        color: AppColors.accentDark,
        decoration: TextDecoration.underline,
      ),
      horizontalRuleDecoration: BoxDecoration(
        border: Border(
          top: BorderSide(color: AppColors.divider, width: 1),
        ),
      ),
    );
  }
}

//  Reusable icon button 

class _ActionIcon extends StatelessWidget {
  final IconData icon;
  final VoidCallback onTap;
  final String tooltip;
  final Color? color;

  const _ActionIcon({
    required this.icon,
    required this.onTap,
    required this.tooltip,
    this.color,
  });

  @override
  Widget build(BuildContext context) {
    return Tooltip(
      message: tooltip,
      child: InkWell(
        onTap: onTap,
        borderRadius: BorderRadius.circular(8),
        child: Padding(
          padding: const EdgeInsets.all(6),
          child: Icon(
            icon,
            size: 16,
            color: color ?? AppColors.textTertiary,
          ),
        ),
      ),
    );
  }
}

// ── Attachment preview row inside sent user bubble ────────────────────────

class _AttachmentPreviewRow extends StatelessWidget {
  final List<ChatAttachment> attachments;

  const _AttachmentPreviewRow({required this.attachments});

  @override
  Widget build(BuildContext context) {
    return Wrap(
      spacing: 6,
      runSpacing: 6,
      alignment: WrapAlignment.end,
      children: attachments.map((a) => _buildTile(context, a)).toList(),
    );
  }

  Widget _buildTile(BuildContext context, ChatAttachment attachment) {
    if (attachment.type == ChatAttachmentType.image) {
      final previewBytes = attachment.thumbnail ?? attachment.bytes;
      return GestureDetector(
        onTap: () =>
            FullScreenImageViewer.open(context, bytes: attachment.bytes),
        child: ClipRRect(
          borderRadius: BorderRadius.circular(8),
          child: Image.memory(
            previewBytes,
            width: 64,
            height: 64,
            fit: BoxFit.cover,
            errorBuilder: (_, _, _) => _docChip(attachment),
          ),
        ),
      );
    }
    return _docChip(attachment);
  }

  Widget _docChip(ChatAttachment attachment) {
    final ext = attachment.fileName.contains('.')
        ? attachment.fileName.split('.').last.toUpperCase()
        : '?';
    return Container(
      height: 32,
      padding: const EdgeInsets.symmetric(horizontal: 8),
      decoration: BoxDecoration(
        color: AppColors.surfaceVariant,
        borderRadius: BorderRadius.circular(8),
        border: Border.all(color: AppColors.glassBorderDim),
      ),
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          const Icon(Icons.description_outlined, size: 14, color: AppColors.textSecondary),
          const SizedBox(width: 4),
          Text(
            ext,
            style: const TextStyle(
              color: AppColors.textTertiary,
              fontSize: 11,
              fontWeight: FontWeight.w600,
            ),
          ),
        ],
      ),
    );
  }
}
