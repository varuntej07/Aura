import 'dart:async';

import 'package:flutter/material.dart';

const _loadingMessages = [
  'pondering the cosmos...',
  'grinding it all together...',
  'consulting my inner oracle...',
  'connecting the dots...',
  'cooking something up...',
  'untangling the threads...',
  'channeling the signal...',
  'almost got it...',
];

/// Shown while a streaming SSE response is in progress.
///
/// Behaviour:
///   - Before first text arrives: rotates [_loadingMessages] every 2.5s with a
///     fade. If [thinkingMessage] is non-null (tool status from backend), shows
///     that instead of the rotating messages.
///   - Once text starts streaming: renders [streamingText] with a blinking ▍
///     cursor appended.
///   - When [isLoading] becomes false the cursor stops blinking (stream done).
class StreamingMessageBubble extends StatefulWidget {
  final String streamingText;
  final String? thinkingMessage;
  final bool isLoading;

  const StreamingMessageBubble({
    super.key,
    required this.streamingText,
    required this.isLoading,
    this.thinkingMessage,
  });

  @override
  State<StreamingMessageBubble> createState() => _StreamingMessageBubbleState();
}

class _StreamingMessageBubbleState extends State<StreamingMessageBubble>
    with TickerProviderStateMixin {
  late AnimationController _cursorController;
  late AnimationController _fadeController;
  late Animation<double> _fadeAnimation;

  int _loadingIndex = 0;
  Timer? _rotateTimer;

  @override
  void initState() {
    super.initState();

    _cursorController = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 600),
    )..repeat(reverse: true);

    _fadeController = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 400),
      value: 1.0,
    );
    _fadeAnimation = CurvedAnimation(
      parent: _fadeController,
      curve: Curves.easeInOut,
    );

    _startRotation();
  }

  void _startRotation() {
    _rotateTimer?.cancel();
    _rotateTimer = Timer.periodic(const Duration(milliseconds: 2500), (_) async {
      if (!mounted) return;
      // Fade out
      await _fadeController.reverse();
      if (!mounted) return;
      setState(() {
        _loadingIndex = (_loadingIndex + 1) % _loadingMessages.length;
      });
      // Fade in
      await _fadeController.forward();
    });
  }

  @override
  void didUpdateWidget(StreamingMessageBubble oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (!widget.isLoading && _cursorController.isAnimating) {
      _cursorController.stop();
    } else if (widget.isLoading && !_cursorController.isAnimating) {
      _cursorController.repeat(reverse: true);
    }
  }

  @override
  void dispose() {
    _cursorController.dispose();
    _fadeController.dispose();
    _rotateTimer?.cancel();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final colorScheme = theme.colorScheme;

    return Align(
      alignment: Alignment.centerLeft,
      child: Container(
        constraints: BoxConstraints(
          maxWidth: MediaQuery.of(context).size.width * 0.78,
        ),
        margin: const EdgeInsets.symmetric(horizontal: 12, vertical: 4),
        padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 10),
        decoration: BoxDecoration(
          gradient: const LinearGradient(
            begin: Alignment.topLeft,
            end: Alignment.bottomRight,
            colors: [Color(0x14FFFFFF), Color(0x08FFFFFF)],
          ),
          borderRadius: const BorderRadius.only(
            topLeft: Radius.circular(4),
            topRight: Radius.circular(18),
            bottomLeft: Radius.circular(18),
            bottomRight: Radius.circular(18),
          ),
          border: Border.all(color: Color(0x1AFFFFFF), width: 1),
        ),
        child: widget.streamingText.isEmpty
            ? _buildLoadingLabel(theme)
            : _buildStreamingText(theme),
      ),
    );
  }

  Widget _buildLoadingLabel(ThemeData theme) {
    final label = widget.thinkingMessage ?? _loadingMessages[_loadingIndex];
    final isToolMessage = widget.thinkingMessage != null;

    return Row(
      mainAxisSize: MainAxisSize.min,
      children: [
        SizedBox(
          width: 14,
          height: 14,
          child: CircularProgressIndicator(
            strokeWidth: 1.5,
            color: theme.colorScheme.onSurfaceVariant.withValues(alpha: 0.6),
          ),
        ),
        const SizedBox(width: 8),
        if (isToolMessage)
          Flexible(
            child: Text(
              label,
              overflow: TextOverflow.ellipsis,
              style: theme.textTheme.bodyMedium?.copyWith(
                color: theme.colorScheme.onSurfaceVariant,
                fontStyle: FontStyle.italic,
              ),
            ),
          )
        else
          Flexible(
            child: FadeTransition(
              opacity: _fadeAnimation,
              child: Text(
                label,
                overflow: TextOverflow.ellipsis,
                style: theme.textTheme.bodyMedium?.copyWith(
                  color: theme.colorScheme.onSurfaceVariant,
                  fontStyle: FontStyle.italic,
                ),
              ),
            ),
          ),
      ],
    );
  }

  Widget _buildStreamingText(ThemeData theme) {
    return AnimatedBuilder(
      animation: _cursorController,
      builder: (context, _) {
        final showCursor = widget.isLoading && _cursorController.value > 0.5;
        return Text.rich(
          TextSpan(
            children: [
              TextSpan(
                text: widget.streamingText,
                style: theme.textTheme.bodyMedium?.copyWith(
                  color: theme.colorScheme.onSurface,
                ),
              ),
              if (showCursor)
                TextSpan(
                  text: '▍',
                  style: theme.textTheme.bodyMedium?.copyWith(
                    color: theme.colorScheme.primary,
                    fontWeight: FontWeight.w600,
                  ),
                ),
            ],
          ),
        );
      },
    );
  }
}
