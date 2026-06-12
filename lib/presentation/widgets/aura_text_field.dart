import 'package:flutter/material.dart';
import '../../core/theme/app_colors.dart';

class AuraTextField extends StatelessWidget {
  final TextEditingController controller;
  final FocusNode? focusNode;
  final String hint;
  final bool enabled;
  final ValueChanged<String>? onSubmitted;

  const AuraTextField({
    super.key,
    required this.controller,
    this.focusNode,
    this.hint = 'Ask Aura anything...',
    this.enabled = true,
    this.onSubmitted,
  });

  @override
  Widget build(BuildContext context) {
    return Container(
      decoration: BoxDecoration(
        color: AppColors.surface,
        borderRadius: BorderRadius.circular(26),
        border: Border.all(color: AppColors.border),
      ),
      child: TextField(
        controller: controller,
        focusNode: focusNode,
        enabled: enabled,
        minLines: 1,
        maxLines: 5,
        style: const TextStyle(
          color: AppColors.textPrimary,
          fontSize: 15,
        ),
        decoration: InputDecoration(
          hintText: hint,
          hintStyle: const TextStyle(
            color: AppColors.textTertiary,
            fontSize: 15,
          ),
          border: InputBorder.none,
          enabledBorder: InputBorder.none,
          focusedBorder: InputBorder.none,
          disabledBorder: InputBorder.none,
          errorBorder: InputBorder.none,
          filled: false,
          contentPadding: const EdgeInsets.symmetric(
            horizontal: 16,
            vertical: 12,
          ),
        ),
        // Return key inserts a newline; sending is done via the send button.
        textInputAction: TextInputAction.newline,
        keyboardType: TextInputType.multiline,
        onSubmitted: (value) {
          if (value.trim().isNotEmpty) {
            onSubmitted?.call(value);
          }
        },
      ),
    );
  }
}
