import 'dart:math' as math;

import 'package:flutter/material.dart';

/// Shown while a streaming SSE response is in progress.
///
/// Behaviour:
///   - Before first text arrives, with NO tool call: renders a Messenger-style
///     typing indicator — three dots bouncing in a smooth, staggered loop.
///   - Before first text arrives, WHILE a tool call is narrating
///     ([thinkingMessage] non-null): renders that narration as an italic label
///     with a pulsing dot. Thinking *phrases* only appear for real tool calls.
///   - Once text starts streaming: renders [streamingText] directly on the
///     canvas (no bubble) with a blinking ▍ cursor appended.
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
    with SingleTickerProviderStateMixin {
  late AnimationController _cursorController;

  @override
  void initState() {
    super.initState();
    _cursorController = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 600),
    )..repeat(reverse: true);
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
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return widget.streamingText.isEmpty
        ? _buildThinkingIndicator(context, theme)
        : _buildStreamingBubble(context, theme);
  }

  /// Typing dots by default; a narration phrase only when a tool call is active.
  Widget _buildThinkingIndicator(BuildContext context, ThemeData theme) {
    final narration = widget.thinkingMessage;
    return Align(
      alignment: Alignment.centerLeft,
      child: Padding(
        padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 8),
        child: narration != null && narration.isNotEmpty
            ? _buildToolNarration(theme, narration)
            : const _TypingDots(),
      ),
    );
  }

  /// Tool-call narration: a pulsing dot + the backend's italic status text.
  Widget _buildToolNarration(ThemeData theme, String label) {
    final textStyle = theme.textTheme.bodySmall?.copyWith(
      color: theme.colorScheme.onSurfaceVariant.withValues(alpha: 0.65),
      fontStyle: FontStyle.italic,
    );
    return Row(
      mainAxisSize: MainAxisSize.min,
      crossAxisAlignment: CrossAxisAlignment.center,
      children: [
        AnimatedBuilder(
          animation: _cursorController,
          builder: (_, _) => Container(
            width: 6,
            height: 6,
            decoration: BoxDecoration(
              shape: BoxShape.circle,
              color: theme.colorScheme.primary.withValues(
                alpha: 0.35 + (_cursorController.value * 0.55),
              ),
            ),
          ),
        ),
        const SizedBox(width: 8),
        Flexible(child: Text(label, style: textStyle)),
      ],
    );
  }

  /// Streaming text renders straight onto the canvas — no container or border —
  /// matching the finalized assistant message in BuddyResponseBubble.
  Widget _buildStreamingBubble(BuildContext context, ThemeData theme) {
    return Align(
      alignment: Alignment.centerLeft,
      child: Container(
        margin: const EdgeInsets.symmetric(vertical: 4),
        padding: const EdgeInsets.symmetric(vertical: 2),
        child: _buildStreamingText(theme),
      ),
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

/// Messenger-style typing indicator: three dots that bounce in a smooth,
/// staggered loop. Shown while Buddy is composing with no tool call running.
class _TypingDots extends StatefulWidget {
  const _TypingDots();

  @override
  State<_TypingDots> createState() => _TypingDotsState();
}

class _TypingDotsState extends State<_TypingDots>
    with SingleTickerProviderStateMixin {
  late final AnimationController _controller = AnimationController(
    vsync: this,
    duration: const Duration(milliseconds: 1200),
  )..repeat();

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final base = Theme.of(context).colorScheme.onSurfaceVariant;
    return SizedBox(
      height: 16,
      child: AnimatedBuilder(
        animation: _controller,
        builder: (context, _) {
          return Row(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.center,
            children: List.generate(3, (i) {
              // Stagger each dot by ~0.18 of the cycle so they ripple, and give
              // each a single smooth up-hump in the first half of its cycle.
              final phase = (_controller.value + i * 0.18) % 1.0;
              final hump =
                  phase < 0.5 ? math.sin(phase / 0.5 * math.pi) : 0.0;
              return Padding(
                padding: EdgeInsets.only(right: i == 2 ? 0 : 5),
                child: Transform.translate(
                  offset: Offset(0, -3.5 * hump),
                  child: Container(
                    width: 6,
                    height: 6,
                    decoration: BoxDecoration(
                      shape: BoxShape.circle,
                      color: base.withValues(alpha: 0.35 + 0.55 * hump),
                    ),
                  ),
                ),
              );
            }),
          );
        },
      ),
    );
  }
}
