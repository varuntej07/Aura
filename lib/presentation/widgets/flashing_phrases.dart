import 'package:flutter/foundation.dart';
import 'package:flutter/widgets.dart';

/// One thing the face shows: a phrase, and optionally the logo it belongs to.
class FlashingItem {
  final String text;

  /// Asset path, or empty for a phrase that stands on its own.
  final String iconAsset;

  const FlashingItem({required this.text, this.iconAsset = ''});
}

/// A card face that cycles through its phrases, one at a time.
///
/// Four of these run at once on the home screen, above an orb that is already
/// breathing and rippling, so the cost model matters more than the effect. Two
/// things keep it cheap:
///
/// - The cross-fade is built from [FadeTransition] and [SlideTransition], which
///   animate inside their own render objects. No widget rebuilds while a phrase
///   moves; only the two [Text] layers repaint, inside a [RepaintBoundary].
///   [AnimatedSwitcher] was the obvious choice and is the wrong one: it re-runs
///   layout on both children every cycle.
/// - There is exactly one `setState` per phrase change, not per frame.
///
/// Unlike [ShimmerSweep], this honours the platform's reduced-motion setting: it
/// simply renders the current phrase and never starts a controller. Nothing is
/// lost, because every phrase is also listed on the opened card.
class FlashingPhrases extends StatefulWidget {
  final List<FlashingItem> items;
  final TextStyle style;
  final int maxLines;

  /// How the phrase sits in the space it is given. Centred on the card faces, so
  /// a one-word phrase and a three-line one occupy the same visual spot instead
  /// of the text jumping to the top of the card as it rotates.
  final Alignment alignment;
  final TextAlign textAlign;

  /// The phrase currently on screen. The deck reads this when a card is tapped so
  /// the card opens on the phrase the user was actually looking at.
  ///
  /// A [ValueNotifier] rather than a callback: a callback would have to
  /// `setState` in the deck every few seconds and rebuild four cards to move one
  /// line of text.
  final ValueNotifier<int> index;

  /// Held true while any card is open, so the phrase behind the opened card stays
  /// the one that was tapped.
  final ValueListenable<bool> paused;

  const FlashingPhrases({
    super.key,
    required this.items,
    required this.style,
    required this.index,
    required this.paused,
    this.maxLines = 3,
    this.alignment = Alignment.center,
    this.textAlign = TextAlign.center,
  });

  @override
  State<FlashingPhrases> createState() => _FlashingPhrasesState();
}

class _FlashingPhrasesState extends State<FlashingPhrases>
    with SingleTickerProviderStateMixin {
  /// Hold, then hand over. The hold is long enough to read a phrase without
  /// hurrying and short enough that a four-phrase card gets through itself while
  /// someone is still looking at the screen.
  static const Duration _cycle = Duration(milliseconds: 3000);
  static const double _handoverFraction = 0.135; // ~400ms of the cycle

  late final AnimationController _controller;
  late final Animation<double> _outgoing;
  late final Animation<double> _incoming;
  late final Animation<Offset> _rise;

  int _current = 0;
  int _previous = 0;
  bool _motionDisabled = false;

  @override
  void initState() {
    super.initState();
    _controller = AnimationController(vsync: this, duration: _cycle);
    const handover = Interval(0, _handoverFraction, curve: Curves.easeOutCubic);
    _outgoing = Tween<double>(begin: 1, end: 0).animate(
      CurvedAnimation(parent: _controller, curve: handover),
    );
    _incoming = Tween<double>(begin: 0, end: 1).animate(
      CurvedAnimation(parent: _controller, curve: handover),
    );
    _rise = Tween<Offset>(
      begin: const Offset(0, 0.35),
      end: Offset.zero,
    ).animate(CurvedAnimation(parent: _controller, curve: handover));

    _controller.addStatusListener(_onCycleComplete);
    widget.paused.addListener(_applyPause);
  }

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    _motionDisabled = MediaQuery.disableAnimationsOf(context);
    _applyPause();
  }

  @override
  void didUpdateWidget(FlashingPhrases oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.paused != widget.paused) {
      oldWidget.paused.removeListener(_applyPause);
      widget.paused.addListener(_applyPause);
    }
    // A new deck, or a phrase consumed after a confirm. Restart from the top
    // rather than leave an index pointing past the end of a shorter list.
    if (_signature(oldWidget.items) != _signature(widget.items)) {
      _current = 0;
      _previous = 0;
      widget.index.value = 0;
      _controller.value = 0;
      _applyPause();
    }
  }

  void _onCycleComplete(AnimationStatus status) {
    if (status != AnimationStatus.completed || !mounted) return;
    if (widget.items.length < 2) return;
    setState(() {
      _previous = _current;
      _current = (_current + 1) % widget.items.length;
    });
    widget.index.value = _current;
    _controller.forward(from: 0);
  }

  /// Identity of the list as rendered, so a genuinely new set restarts the
  /// rotation while an identical rebuild does not.
  static String _signature(List<FlashingItem> items) =>
      items.map((i) => '${i.iconAsset}~${i.text}').join('|');

  /// The only place the controller is started or stopped, so "should this be
  /// running?" has exactly one answer.
  void _applyPause() {
    if (!mounted) return;
    final shouldRun = !_motionDisabled &&
        !widget.paused.value &&
        widget.items.length > 1;
    if (shouldRun) {
      if (!_controller.isAnimating) _controller.forward(from: _controller.value);
    } else {
      _controller.stop();
    }
  }

  @override
  void deactivate() {
    _controller.stop();
    super.deactivate();
  }

  @override
  void dispose() {
    widget.paused.removeListener(_applyPause);
    _controller.removeStatusListener(_onCycleComplete);
    _controller.dispose();
    super.dispose();
  }

  Widget _line(FlashingItem item) {
    final text = Text(
      item.text,
      maxLines: widget.maxLines,
      overflow: TextOverflow.ellipsis,
      textAlign: widget.textAlign,
      style: widget.style,
    );
    if (item.iconAsset.isEmpty) return text;
    // Shrink-wrapped and Flexible so a long name still ellipsises instead of
    // pushing the logo off the card.
    return Row(
      mainAxisSize: MainAxisSize.min,
      mainAxisAlignment: MainAxisAlignment.center,
      children: [
        Container(
          width: 26,
          height: 26,
          decoration: BoxDecoration(
            color: const Color(0xFFFFFFFF),
            borderRadius: BorderRadius.circular(7),
          ),
          padding: const EdgeInsets.all(3),
          child: Image.asset(item.iconAsset),
        ),
        const SizedBox(width: 9),
        Flexible(child: text),
      ],
    );
  }

  @override
  Widget build(BuildContext context) {
    final items = widget.items;
    if (items.isEmpty) return const SizedBox.shrink();

    final current = items[_current.clamp(0, items.length - 1)];
    // One phrase, or motion turned off: render it plainly. No controller, no
    // stack, nothing ticking.
    if (items.length < 2 || _motionDisabled) {
      return SizedBox(
        width: double.infinity,
        // Takes the full width it is offered, or [alignment] would only centre
        // the text inside its own shrink-wrapped box and it would still sit
        // wherever the parent put that box.
        child: Align(alignment: widget.alignment, child: _line(current)),
      );
    }

    final previous = items[_previous.clamp(0, items.length - 1)];
    return RepaintBoundary(
      child: Stack(
        alignment: widget.alignment,
        children: [
          const SizedBox(width: double.infinity),
          // The outgoing phrase only exists during the handover; afterwards it is
          // fully transparent and costs a paint of nothing.
          FadeTransition(
            opacity: _outgoing,
            child: _line(previous),
          ),
          FadeTransition(
            opacity: _incoming,
            child: SlideTransition(position: _rise, child: _line(current)),
          ),
        ],
      ),
    );
  }
}
