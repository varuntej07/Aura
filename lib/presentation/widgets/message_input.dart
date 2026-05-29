import 'package:flutter/material.dart';

import '../../core/theme/app_colors.dart';
import '../../data/models/chat_attachment.dart';
import '../../data/services/attachment_processor.dart';
import 'attachment_thumbnail_strip.dart';
import 'aura_text_field.dart';

/// Text input bar at the bottom of any chat screen.
/// Owns its [TextEditingController] unless [controller] is provided externally.
/// Pass an external controller when a sibling widget (e.g. suggestion pills)
/// needs to write into the field.
class MessageInput extends StatefulWidget {
  final bool isLoading;
  final String hint;
  final void Function(String text, List<ChatAttachment> attachments) onSend;
  final VoidCallback? onStop;
  final TextEditingController? controller;
  final double extraBottomPadding;
  final bool allowAttachments;

  const MessageInput({
    super.key,
    required this.onSend,
    this.isLoading = false,
    this.hint = 'Message…',
    this.onStop,
    this.controller,
    this.extraBottomPadding = 0,
    this.allowAttachments = true,
  });

  @override
  State<MessageInput> createState() => _MessageInputState();
}

class _MessageInputState extends State<MessageInput> {
  late final TextEditingController _controller;
  late final bool _ownsController;
  final _processor = AttachmentProcessor();
  final _pendingAttachments = <ChatAttachment>[];
  bool _isProcessingAttachment = false;

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
    if (widget.isLoading) return;
    if (text.isEmpty && _pendingAttachments.isEmpty) return;
    final attachments = List<ChatAttachment>.from(_pendingAttachments);
    _controller.clear();
    setState(() => _pendingAttachments.clear());
    widget.onSend(text, attachments);
  }

  void _removeAttachment(String id) {
    setState(() => _pendingAttachments.removeWhere((a) => a.id == id));
  }

  void _showAttachmentSheet() {
    showModalBottomSheet<void>(
      context: context,
      backgroundColor: AppColors.surface,
      shape: const RoundedRectangleBorder(
        borderRadius: BorderRadius.vertical(top: Radius.circular(20)),
      ),
      builder: (_) => SafeArea(
        child: Padding(
          padding: const EdgeInsets.symmetric(vertical: 12),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              Container(
                width: 36,
                height: 4,
                margin: const EdgeInsets.only(bottom: 16),
                decoration: BoxDecoration(
                  color: AppColors.glassBorderDim,
                  borderRadius: BorderRadius.circular(2),
                ),
              ),
              _SheetOption(
                icon: Icons.photo_camera_outlined,
                label: 'Camera',
                onTap: () {
                  Navigator.pop(context);
                  _pickFromCamera();
                },
              ),
              _SheetOption(
                icon: Icons.photo_library_outlined,
                label: 'Photos',
                onTap: () {
                  Navigator.pop(context);
                  _pickFromGallery();
                },
              ),
              _SheetOption(
                icon: Icons.attach_file_rounded,
                label: 'Files',
                onTap: () {
                  Navigator.pop(context);
                  _pickFile();
                },
              ),
              const SizedBox(height: 8),
            ],
          ),
        ),
      ),
    );
  }

  Future<void> _pickFromCamera() async {
    setState(() => _isProcessingAttachment = true);
    try {
      final xFile = await _processor.pickImageFromCamera();
      if (xFile == null) return;
      final result = await _processor.processPickedImage(xFile, _pendingAttachments);
      _handleResult(result);
    } catch (_) {
      _showError('Could not load image. Please try again.');
    } finally {
      if (mounted) setState(() => _isProcessingAttachment = false);
    }
  }

  Future<void> _pickFromGallery() async {
    setState(() => _isProcessingAttachment = true);
    try {
      final xFiles = await _processor.pickImagesFromGallery();
      if (xFiles.isEmpty) return;
      final results = await _processor.processPickedImages(xFiles, _pendingAttachments);
      for (final result in results) {
        _handleResult(result);
      }
    } catch (_) {
      _showError('Could not load images. Please try again.');
    } finally {
      if (mounted) setState(() => _isProcessingAttachment = false);
    }
  }

  Future<void> _pickFile() async {
    setState(() => _isProcessingAttachment = true);
    try {
      final result = await _processor.pickFiles();
      if (result == null) return;
      for (final platformFile in result.files) {
        final processed = await _processor.processPlatformFile(platformFile, _pendingAttachments);
        _handleResult(processed);
      }
    } catch (_) {
      _showError('Could not open file. Please try again.');
    } finally {
      if (mounted) setState(() => _isProcessingAttachment = false);
    }
  }

  void _handleResult(AttachmentProcessingResult result) {
    if (!mounted) return;
    if (result.attachment != null) {
      setState(() => _pendingAttachments.add(result.attachment!));
    } else if (result.error != null) {
      _showError(result.error!);
    }
  }

  void _showError(String message) {
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(
        content: Text(message),
        behavior: SnackBarBehavior.floating,
        backgroundColor: AppColors.surface,
        duration: const Duration(seconds: 3),
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final bool canAttach = !widget.isLoading && !_isProcessingAttachment;

    return RepaintBoundary(
      child: Container(
        decoration: BoxDecoration(
          gradient: const LinearGradient(
            begin: Alignment.topCenter,
            end: Alignment.bottomCenter,
            colors: [Color(0x00080812), Color(0xCC080812)],
          ),
          border: Border(
            top: BorderSide(color: AppColors.glassBorderDim, width: 0.5),
          ),
        ),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            if (_pendingAttachments.isNotEmpty)
              AttachmentThumbnailStrip(
                attachments: _pendingAttachments,
                onRemove: _removeAttachment,
                isLoading: widget.isLoading,
              ),
            Padding(
              padding: EdgeInsets.fromLTRB(12, 8, 16, 16 + widget.extraBottomPadding),
              child: Row(
                crossAxisAlignment: CrossAxisAlignment.end,
                children: [
                  if (widget.allowAttachments) ...[
                    _AttachButton(
                      onTap: canAttach ? _showAttachmentSheet : null,
                      isProcessing: _isProcessingAttachment,
                    ),
                    const SizedBox(width: 8),
                  ],
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
          ],
        ),
      ),
    );
  }
}

// ── Attachment button ──────────────────────────────────────────────────────

class _AttachButton extends StatelessWidget {
  final VoidCallback? onTap;
  final bool isProcessing;

  const _AttachButton({this.onTap, required this.isProcessing});

  @override
  Widget build(BuildContext context) {
    return GestureDetector(
      onTap: onTap,
      child: AnimatedOpacity(
        opacity: onTap != null ? 1.0 : 0.4,
        duration: const Duration(milliseconds: 150),
        child: Container(
          width: 44,
          height: 44,
          decoration: BoxDecoration(
            color: AppColors.surfaceVariant,
            shape: BoxShape.circle,
            border: Border.all(color: AppColors.glassBorderDim),
          ),
          child: isProcessing
              ? const Padding(
                  padding: EdgeInsets.all(12),
                  child: CircularProgressIndicator(
                    strokeWidth: 2,
                    valueColor: AlwaysStoppedAnimation(AppColors.textSecondary),
                  ),
                )
              : const Icon(
                  Icons.add_rounded,
                  size: 22,
                  color: AppColors.textSecondary,
                ),
        ),
      ),
    );
  }
}

// ── Bottom sheet option tile ───────────────────────────────────────────────

class _SheetOption extends StatelessWidget {
  final IconData icon;
  final String label;
  final VoidCallback onTap;

  const _SheetOption({
    required this.icon,
    required this.label,
    required this.onTap,
  });

  @override
  Widget build(BuildContext context) {
    return InkWell(
      onTap: onTap,
      child: Padding(
        padding: const EdgeInsets.symmetric(horizontal: 24, vertical: 14),
        child: Row(
          children: [
            Container(
              width: 40,
              height: 40,
              decoration: BoxDecoration(
                color: AppColors.surfaceVariant,
                borderRadius: BorderRadius.circular(10),
              ),
              child: Icon(icon, size: 20, color: AppColors.textSecondary),
            ),
            const SizedBox(width: 16),
            Text(
              label,
              style: const TextStyle(
                color: AppColors.textPrimary,
                fontSize: 16,
                fontWeight: FontWeight.w500,
              ),
            ),
          ],
        ),
      ),
    );
  }
}

// ── Send / Stop buttons ────────────────────────────────────────────────────

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
