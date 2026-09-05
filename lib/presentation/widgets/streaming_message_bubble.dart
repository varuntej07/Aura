import 'dart:async';
import 'dart:math' as math;

import 'package:flutter/material.dart';
import 'package:flutter_markdown_plus/flutter_markdown_plus.dart';
import 'package:url_launcher/url_launcher.dart';

import 'buddy_markdown_style.dart';

/// Shown while a streaming SSE response is in progress.
///
/// Behaviour:
///   - Before first text arrives, with NO tool call: renders a Messenger-style
///     typing indicator — three dots bouncing in a smooth, staggered loop.
///   - Before first text arrives, WHILE a tool call is narrating
///     ([thinkingMessage] non-null): renders that narration as an italic label
///     with a pulsing dot. Thinking *phrases* only appear for real tool calls.
///   - Once text starts streaming: renders [streamingText] directly on the
///     canvas (no bubble) as live markdown, with the same stylesheet the
///     finalized BuddyResponseBubble uses, so there is no reflow jump when the
///     persisted message replaces this slot. Re-parses are coalesced to one per
///     ~90ms; the growing text itself is the streaming affordance (no cursor,
///     matching the desktop client).
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

  // Markdown re-parse coalescing: deltas arrive many times per second and each
  // MarkdownBody build re-parses the whole accumulated text, so parses are
  // capped to one per interval and the previous parse is shown in between.
  static const _markdownParseInterval = Duration(milliseconds: 90);
  String _renderedText = '';
  Widget? _renderedMarkdown;
  DateTime _lastParseAt = DateTime.fromMillisecondsSinceEpoch(0);
  Timer? _pendingParse;

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
    _pendingParse?.cancel();
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
    final text = widget.streamingText;
    if (_renderedMarkdown == null || text != _renderedText) {
      final now = DateTime.now();
      // Once the stream ends, parse unconditionally so the final text is shown
      // before the persisted bubble replaces this slot.
      if (!widget.isLoading ||
          now.difference(_lastParseAt) >= _markdownParseInterval) {
        _renderedText = text;
        _lastParseAt = now;
        _renderedMarkdown = MarkdownBody(
          data: text,
          selectable: false,
          onTapLink: (text, href, title) {
            if (href != null) {
              launchUrl(Uri.parse(href), mode: LaunchMode.externalApplication);
            }
          },
          styleSheet: buddyMarkdownStyleSheet(),
        );
      } else {
        _pendingParse ??= Timer(_markdownParseInterval, () {
          _pendingParse = null;
          if (mounted) setState(() {});
        });
      }
    }
    return _renderedMarkdown!;
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
