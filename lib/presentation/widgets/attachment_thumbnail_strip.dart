import 'package:flutter/material.dart';

import '../../core/theme/app_colors.dart';
import '../../data/models/chat_attachment.dart';

/// Horizontal scrollable strip of attachment previews shown above the input
/// field after the user selects files. Each thumbnail has an × remove button.
/// Disabled while a response is streaming (isLoading = true).
class AttachmentThumbnailStrip extends StatelessWidget {
  final List<ChatAttachment> attachments;
  final void Function(String attachmentId) onRemove;
  final bool isLoading;

  const AttachmentThumbnailStrip({
    super.key,
    required this.attachments,
    required this.onRemove,
    this.isLoading = false,
  });

  @override
  Widget build(BuildContext context) {
    return SizedBox(
      height: 84,
      child: ListView.separated(
        scrollDirection: Axis.horizontal,
        padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 6),
        itemCount: attachments.length,
        separatorBuilder: (_, _) => const SizedBox(width: 8),
        itemBuilder: (_, i) => _AttachmentTile(
          attachment: attachments[i],
          onRemove: isLoading ? null : () => onRemove(attachments[i].id),
        ),
      ),
    );
  }
}

class _AttachmentTile extends StatelessWidget {
  final ChatAttachment attachment;
  final VoidCallback? onRemove;

  const _AttachmentTile({required this.attachment, this.onRemove});

  @override
  Widget build(BuildContext context) {
    return Stack(
      clipBehavior: Clip.none,
      children: [
        _buildPreview(),
        if (onRemove != null)
          Positioned(
            top: -6,
            right: -6,
            child: GestureDetector(
              onTap: onRemove,
              child: Container(
                width: 20,
                height: 20,
                decoration: BoxDecoration(
                  color: AppColors.surface,
                  shape: BoxShape.circle,
                  border: Border.all(color: AppColors.glassBorderDim, width: 1),
                ),
                child: const Icon(
                  Icons.close_rounded,
                  size: 12,
                  color: AppColors.textSecondary,
                ),
              ),
            ),
          ),
      ],
    );
  }

  Widget _buildPreview() {
    if (attachment.type == ChatAttachmentType.image) {
      final previewBytes = attachment.thumbnail ?? attachment.bytes;
      return ClipRRect(
        borderRadius: BorderRadius.circular(10),
        child: Image.memory(
          previewBytes,
          width: 72,
          height: 72,
          fit: BoxFit.cover,
          errorBuilder: (_, __, ___) => _documentTile(),
        ),
      );
    }
    return _documentTile();
  }

  Widget _documentTile() {
    final name = attachment.fileName;
    final ext = name.contains('.') ? name.split('.').last.toUpperCase() : '?';
    return Container(
      width: 72,
      height: 72,
      decoration: BoxDecoration(
        color: AppColors.surfaceVariant,
        borderRadius: BorderRadius.circular(10),
        border: Border.all(color: AppColors.glassBorderDim),
      ),
      child: Column(
        mainAxisAlignment: MainAxisAlignment.center,
        children: [
          const Icon(Icons.description_outlined, size: 24, color: AppColors.textSecondary),
          const SizedBox(height: 4),
          Text(
            ext,
            style: const TextStyle(
              color: AppColors.textTertiary,
              fontSize: 10,
              fontWeight: FontWeight.w600,
            ),
          ),
          const SizedBox(height: 2),
          Padding(
            padding: const EdgeInsets.symmetric(horizontal: 4),
            child: Text(
              name.length > 10 ? '${name.substring(0, 8)}…' : name,
              style: const TextStyle(color: AppColors.textTertiary, fontSize: 9),
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
            ),
          ),
        ],
      ),
    );
  }
}
