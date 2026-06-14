/// Immutable snapshot of the in-flight assistant turn.
///
/// Published on every streamed token through a `ValueNotifier` so that only the
/// live streaming bubble repaints — the finalized message list and the rest of
/// the chat screen stay put (no per-token `notifyListeners`). [thinkingMessage]
/// is non-null only while a tool call is narrating (see `ChatViewModel`'s
/// `ToolThinkingEvent` handling); [text] is the assistant copy streamed so far.
class StreamingSnapshot {
  final String text;
  final String? thinkingMessage;

  const StreamingSnapshot({this.text = '', this.thinkingMessage});

  /// Reset value: no text, no tool narration — renders the typing indicator.
  static const empty = StreamingSnapshot();

  @override
  bool operator ==(Object other) =>
      other is StreamingSnapshot &&
      other.text == text &&
      other.thinkingMessage == thinkingMessage;

  @override
  int get hashCode => Object.hash(text, thinkingMessage);
}
