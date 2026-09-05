import 'package:flutter/material.dart';
import 'package:flutter_markdown_plus/flutter_markdown_plus.dart';

import '../../core/theme/app_colors.dart';

/// The one markdown theme for assistant text, shared by the finalized bubble
/// (BuddyResponseBubble) and the live streaming bubble so the reply looks
/// identical while it streams and after it lands. Extracted from
/// BuddyResponseBubble when the streaming bubble started rendering markdown.
MarkdownStyleSheet buddyMarkdownStyleSheet() {
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
