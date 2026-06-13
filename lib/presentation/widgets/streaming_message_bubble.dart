import 'dart:async';

import 'package:flutter/material.dart';

const _loadingMessagesByContext = <String, List<String>>{
  'sports': [
    'checking the scorecard...',
    'scanning the pitch...',
    'reading the match data...',
    'pulling up the highlights...',
    'watching the replays...',
    'tallying the runs...',
    'scouting the field...',
    'checking live scores...',
    'reviewing the innings...',
    'running the stats...',
  ],
  'technews': [
    'scanning the wire...',
    'parsing the stack...',
    'pulling the latest commits...',
    'reading the changelog...',
    'checking the release notes...',
    'scanning hacker news...',
    'pulling research papers...',
    'indexing the feed...',
    'loading the diff...',
  ],
  'posts': [
    'drafting your voice...',
    'finding the angle...',
    'sharpening the take...',
    'writing the hook...',
    'warming up the keyboard...',
    'finding the right words...',
    'crafting the draft...',
  ],
  'default': [
    'working on it...',
    'thinking it through...',
    'connecting the dots...',
    'cooking something up...',
    'untangling the threads...',
    'reading the room...',
    'pulling it together...',
    'almost there...',
    'running it through...',
    'locking in...',
    'picking up the signal...',
    'in the zone...',
    'on it...',
    'processing...',
    'just a moment...',
  ],
};

List<String> _messagesForContext(String? contextTag) =>
    _loadingMessagesByContext[contextTag] ??
    _loadingMessagesByContext['default']!;

/// Shown while a streaming SSE response is in progress.
///
/// Behaviour:
///   - Before first text arrives: renders an inline thinking indicator —
///     a pulsing dot + italic rotating label without container or background.
///     If [thinkingMessage] is non-null (tool narration from backend), shows that instead of the rotating messages.
///   - Once text starts streaming: renders [streamingText] inside a glass
///     bubble with a blinking ▍ cursor appended.
///   - When [isLoading] becomes false the cursor stops blinking (stream done).
class StreamingMessageBubble extends StatefulWidget {
  final String streamingText;
  final String? thinkingMessage;
  final bool isLoading;

  /// Selects the loading message set for the current screen context.
  /// Matches keys in [_loadingMessagesByContext]: 'sports', 'technews',
  /// 'posts'. Null falls back to the default general set.
  final String? contextTag;

  const StreamingMessageBubble({
    super.key,
    required this.streamingText,
    required this.isLoading,
    this.thinkingMessage,
    this.contextTag,
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
      final messages = _messagesForContext(widget.contextTag);
      await _fadeController.reverse();
      if (!mounted) return;
      setState(() {
        _loadingIndex = (_loadingIndex + 1) % messages.length;
      });
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
    return widget.streamingText.isEmpty
        ? _buildThinkingIndicator(context, theme)
        : _buildStreamingBubble(context, theme);
  }

  /// Matches the chain-of-thought style used by Claude.ai and Perplexity.
  Widget _buildThinkingIndicator(BuildContext context, ThemeData theme) {
    final messages = _messagesForContext(widget.contextTag);
    final label = widget.thinkingMessage ?? messages[_loadingIndex % messages.length];

    final textStyle = theme.textTheme.bodySmall?.copyWith(
      color: theme.colorScheme.onSurfaceVariant.withValues(alpha: 0.65),
      fontStyle: FontStyle.italic,
    );

    return Align(
      alignment: Alignment.centerLeft,
      child: Padding(
        padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 6),
        child: Row(
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
            widget.thinkingMessage != null
                ? Flexible(child: Text(label, style: textStyle))
                : FadeTransition(
                    opacity: _fadeAnimation,
                    child: Text(label, style: textStyle),
                  ),
          ],
        ),
      ),
    );
  }

  /// Glass bubble — only rendered once text starts arriving.
  Widget _buildStreamingBubble(BuildContext context, ThemeData theme) {
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
            colors: [Color(0x0F2B2A26), Color(0x082B2A26)],
          ),
          borderRadius: const BorderRadius.only(
            topLeft: Radius.circular(4),
            topRight: Radius.circular(18),
            bottomLeft: Radius.circular(18),
            bottomRight: Radius.circular(18),
          ),
          border: Border.all(color: Color(0x1F2B2A26), width: 1),
        ),
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
