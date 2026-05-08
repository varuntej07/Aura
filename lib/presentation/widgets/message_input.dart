import 'package:flutter/material.dart';

import '../../core/theme/app_colors.dart';
import '../../core/theme/glass_card.dart';
import 'aura_text_field.dart';

/// Text input bar at the bottom of any chat screen.
/// Owns its [TextEditingController] unless [controller] is provided externally.
/// Pass an external controller when a sibling widget (e.g. suggestion pills)
/// needs to write into the field.
class MessageInput extends StatefulWidget {
  final bool isLoading;
  final String hint;
  final void Function(String text) onSend;
  final VoidCallback? onStop;
  final TextEditingController? controller;

  const MessageInput({
    super.key,
    required this.onSend,
    this.isLoading = false,
    this.hint = 'Message…',
    this.onStop,
    this.controller,
  });

  @override
  State<MessageInput> createState() => _MessageInputState();
}

class _MessageInputState extends State<MessageInput> {
  late final TextEditingController _controller;
  late final bool _ownsController;

  @override
  void initState() {
    super.initState();
    if (widget.controller != null) {
      _controller = widget.controller!;
      _ownsController = false;
    } else {
      _controller = TextEditingController();
      _ownsController = true;
    }
  }

  @override
  void dispose() {
    if (_ownsController) _controller.dispose();
    super.dispose();
  }

  void _send() {
    final text = _controller.text.trim();
    if (text.isEmpty || widget.isLoading) return;
    _controller.clear();
    widget.onSend(text);
  }

  @override
  Widget build(BuildContext context) {
    return RepaintBoundary(
      child: Container(
        padding: const EdgeInsets.fromLTRB(16, 8, 16, 16),
        decoration: BoxDecoration(
          gradient: const LinearGradient(
            begin: Alignment.topCenter,
            end: Alignment.bottomCenter,
            colors: [Color(0x00080812), Color(0xCC080812)],
          ),
          border: Border(
              top: BorderSide(
                  color: AppColors.glassBorderDim, width: 0.5)),
        ),
        child: Row(
          children: [
            Expanded(
              child: AuraTextField(
                controller: _controller,
                hint: widget.hint,
                enabled: !widget.isLoading,
                onSubmitted: (_) => _send(),
              ),
            ),
            const SizedBox(width: 10),
            widget.isLoading && widget.onStop != null
                ? _StopButton(onTap: widget.onStop!)
                : _SendButton(onTap: _send, enabled: !widget.isLoading),
          ],
        ),
      ),
    );
  }
}

class _SendButton extends StatelessWidget {
  final VoidCallback onTap;
  final bool enabled;

  const _SendButton({required this.onTap, required this.enabled});

  @override
  Widget build(BuildContext context) {
    return GestureDetector(
      onTap: enabled ? onTap : null,
      child: AnimatedOpacity(
        opacity: enabled ? 1.0 : 0.4,
        duration: const Duration(milliseconds: 150),
        child: Container(
          width: 44,
          height: 44,
          decoration: const BoxDecoration(
            color: AppColors.accent,
            shape: BoxShape.circle,
          ),
          child: const Icon(Icons.arrow_upward_rounded, color: Colors.white, size: 20),
        ),
      ),
    );
  }
}

class _StopButton extends StatelessWidget {
  final VoidCallback onTap;

  const _StopButton({required this.onTap});

  @override
  Widget build(BuildContext context) {
    return GestureDetector(
      onTap: onTap,
      child: Container(
        width: 44,
        height: 44,
        decoration: const BoxDecoration(
          color: AppColors.accent,
          shape: BoxShape.circle,
        ),
        child: const Icon(Icons.stop_rounded, color: Colors.white, size: 20),
      ),
    );
  }
}
