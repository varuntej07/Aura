import 'dart:math' as math;

import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:provider/provider.dart';

import '../../core/analytics/funnel_events.dart';
import '../../core/theme/app_colors.dart';
import '../../core/theme/glass_card.dart';
import '../../data/models/home_deck_card.dart';
import '../../data/services/posthog_analytics_service.dart';
import '../viewmodels/home_deck_viewmodel.dart';
import 'deck_card_detail.dart';
import 'flashing_phrases.dart';

/// The cards Buddy throws out of the orb when you open the app.
///
/// The orb is the thrower: each card launches from the orb's position below this
/// layer, arcs upward while spinning, and settles into a loose two-by-two hand.
/// That is the whole point of the motion — a card that slides in from a screen
/// edge reads as a banner, and a card that fades in reads as a list.
///
/// Every card is rebuilt exactly once. The flight runs through AnimatedBuilder
/// with the card content passed as `child`, so sixty frames a second cost four
/// transforms and no widget construction.
class OrbCardDeck extends StatefulWidget {
  /// Fired the instant a card leaves the orb, so the orb can squash in time with
  /// it. Called once per launch, including replacements.
  final VoidCallback onEmit;

  /// A talk card: opens chat with Buddy speaking first.
  final void Function(String openingMessage) onStartTalk;

  /// A briefing or connect card: goes to a screen that already exists.
  final void Function(String route) onNavigate;

  /// Something was actually created, with the line to show the user.
  final void Function(String message) onCreated;

  const OrbCardDeck({
    super.key,
    required this.onEmit,
    required this.onStartTalk,
    required this.onNavigate,
    required this.onCreated,
  });

  @override
  State<OrbCardDeck> createState() => _OrbCardDeckState();
}

/// Where one card comes to rest, in the layer's own coordinates.
class _Slot {
  final Offset topLeft;
  final double angle;
  const _Slot(this.topLeft, this.angle);
}

class _OrbCardDeckState extends State<OrbCardDeck> with TickerProviderStateMixin {
  static const int _slotCount = kHomeDeckVisibleCards;
  static const Duration _flight = Duration(milliseconds: 420);
  static const Duration _stagger = Duration(milliseconds: 250);

  // Deterministic scatter. Random() would re-roll on every rebuild, which reads
  // as the cards twitching rather than as a hand someone dealt.
  static const List<double> _restAngles = [-0.032, 0.026, 0.020, -0.028];
  static const List<double> _spins = [-4.1, 3.4, -3.0, 4.6];
  static const List<double> _nudges = [-2, 2, 2, -2];

  late final List<AnimationController> _throws;
  late final AnimationController _flip;

  /// Slots whose throw has actually been started.
  ///
  /// A card that has never been launched renders at REST, not at the start of a
  /// flight it may never take. Without this, anything that stops the deal from
  /// running (a dropped post-frame callback, a rebuild at the wrong moment)
  /// leaves every card at opacity 0 and scale 0.18 — which on screen is
  /// identical to having no cards at all. The animation is a flourish; it must
  /// never be the thing that decides whether content is visible.
  final Set<int> _launched = {};

  int? _openIndex;
  bool _dealt = false;

  /// Which phrase each slot is currently showing. Written by the faces, read when
  /// one is tapped so the opened square starts on the phrase the user saw.
  late final List<ValueNotifier<int>> _flashIndex;

  /// Held true while a square is open: the phrase behind it must not rotate out
  /// from under the thing the user is looking at.
  final ValueNotifier<bool> _flashPaused = ValueNotifier(false);

  bool get _reducedMotion => MediaQuery.disableAnimationsOf(context);

  @override
  void initState() {
    super.initState();
    _throws = List.generate(
      _slotCount,
      (_) => AnimationController(vsync: this, duration: _flight),
    );
    _flip = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 300),
    );
    _flashIndex = List.generate(_slotCount, (_) => ValueNotifier(0));
  }

  @override
  void dispose() {
    for (final controller in _throws) {
      controller.dispose();
    }
    for (final notifier in _flashIndex) {
      notifier.dispose();
    }
    _flashPaused.dispose();
    _flip.dispose();
    super.dispose();
  }

  /// Deal the opening hand, one card every 250ms.
  Future<void> _deal(int count) async {
    _dealt = true;
    for (var i = 0; i < count && i < _slotCount; i++) {
      if (!mounted) return;
      _launch(i);
      await Future<void>.delayed(_stagger);
    }
  }

  void _launch(int index) {
    if (!mounted || index >= _throws.length) return;
    final hand = context.read<HomeDeckViewModel>().hand;
    if (index < hand.length) _track(FunnelEvents.deckCardThrown, hand[index]);
    widget.onEmit();
    setState(() => _launched.add(index));
    if (_reducedMotion) {
      _throws[index].value = 1;
    } else {
      _throws[index].forward(from: 0);
    }
  }

  void _track(String event, HomeDeckCard card) {
    if (!mounted) return;
    context.read<PostHogAnalyticsService>().trackEvent(
      event,
      properties: {FunnelEvents.propDeckCardKind: card.kind.name},
    );
  }

  List<_Slot> _slots(Size size, double cardWidth, double cardHeight, int count) {
    const sidePadding = 16.0;
    const gutter = 8.0;
    final left = sidePadding;
    final right = size.width - sidePadding - cardWidth;

    // Two cards sit side by side in one row. Stacking them would need two rows
    // of height in a band that was too short for two rows in the first place.
    if (count <= 2) {
      final row = (size.height - cardHeight) / 2;
      return [
        _Slot(Offset(left, row + _nudges[0]), _restAngles[0]),
        _Slot(Offset(right, row + _nudges[1]), _restAngles[1]),
        _Slot(Offset(left, row + _nudges[2]), _restAngles[2]),
        _Slot(Offset(right, row + _nudges[3]), _restAngles[3]),
      ];
    }
    // Centred in the band rather than anchored above the orb: the hand reads as
    // the thing on the screen, with the orb sitting under it, instead of a strip
    // clinging to the bottom edge.
    final gridHeight = cardHeight * 2 + gutter;
    final topRow = (size.height - gridHeight) / 2;
    final bottomRow = topRow + cardHeight + gutter;
    return [
      _Slot(Offset(left, topRow + _nudges[0]), _restAngles[0]),
      _Slot(Offset(right, topRow + _nudges[1]), _restAngles[1]),
      _Slot(Offset(left, bottomRow + _nudges[2]), _restAngles[2]),
      _Slot(Offset(right, bottomRow + _nudges[3]), _restAngles[3]),
    ];
  }

  /// Quadratic bezier from the orb to the slot. The control point sits above the
  /// straight line, which is what makes it an arc instead of a slide.
  Offset _flightPosition(Offset from, Offset to, double t) {
    final lateral = to.dx < from.dx ? -1.0 : 1.0;
    final control = Offset(
      (from.dx + to.dx) / 2 + lateral * 70,
      (from.dy + to.dy) / 2 - 90,
    );
    final inverse = 1 - t;
    return Offset(
      inverse * inverse * from.dx + 2 * inverse * t * control.dx + t * t * to.dx,
      inverse * inverse * from.dy + 2 * inverse * t * control.dy + t * t * to.dy,
    );
  }

  Future<void> _openCard(int index, HomeDeckCard card) async {
    HapticFeedback.selectionClick();
    _track(FunnelEvents.deckCardOpened, card);
    setState(() => _openIndex = index);
    _flashPaused.value = true;
    await showDeckCardDetail(
      context,
      card: card,
      index: index,
      initialSuggestion: _flashIndex[index].value,
      onNavigate: widget.onNavigate,
      onStartTalk: widget.onStartTalk,
      onCreated: widget.onCreated,
    );
    if (!mounted) return;
    setState(() => _openIndex = null);
    _flashPaused.value = false;
  }


  @override
  Widget build(BuildContext context) {
    final vm = context.watch<HomeDeckViewModel>();
    final cards = vm.hand;
    if (!vm.loaded || cards.isEmpty) return const SizedBox.shrink();

    if (!_dealt) {
      WidgetsBinding.instance.addPostFrameCallback((_) {
        if (mounted && !_dealt) _deal(cards.length);
      });
    }

    return LayoutBuilder(
      builder: (context, constraints) {
        final size = Size(constraints.maxWidth, constraints.maxHeight);
        // Two rows of cards have to fit above the orb on a small phone, so the
        // card sheds its subtitle before it sheds its readability.
        // A band this short cannot hold a readable card at all; drawing one
        // would mean a negative or clipped height, which is worse than the
        // screen simply not offering cards here.
        if (size.height < 96 || size.width < 240) {
          return const SizedBox.shrink();
        }

        final compact = size.height < 320;
        // Sized to the band: on a tall screen the cards take the room they have
        // instead of floating in it, capped so they stay cards rather than
        // panels. Clamped at both ends so an unusual band can never produce a
        // negative height.
        // twoRowFit stays the hard ceiling: it is what actually fits two rows in
        // this band, so the 1.15 below raises the caps without letting a tall
        // card overflow into the orb on a short screen.
        final twoRowFit = (size.height - 8) / 2 - 16;
        final cardHeight = size.height < 250
            ? math.max(101.0, math.min(152.0, size.height - 24))
            : (compact ? 124.0 : math.min(193.0, math.max(110.0, twoRowFit)));
        // Below this, two whole cards beat four cropped ones.
        final visibleCount = size.height < 250 ? 2 : cards.length;
        final cardWidth = (size.width - 32 - 8) / 2;
        final slots = _slots(size, cardWidth, cardHeight, visibleCount);
        final launch = Offset(size.width / 2 - cardWidth / 2, size.height + 40);

        return RepaintBoundary(
          child: Stack(
            clipBehavior: Clip.none,
            children: [
              for (var i = 0; i < visibleCount && i < slots.length; i++)
                _buildFlyingCard(
                  index: i,
                  card: cards[i],
                  slot: slots[i],
                  launch: launch,
                  width: cardWidth,
                  height: cardHeight,
                  compact: compact,
                ),
            ],
          ),
        );
      },
    );
  }

  Widget _buildFlyingCard({
    required int index,
    required HomeDeckCard card,
    required _Slot slot,
    required Offset launch,
    required double width,
    required double height,
    required bool compact,
  }) {
    final controller = _throws[index];

    return AnimatedBuilder(
      animation: controller,
      // Built once. The flight only ever changes transforms around this subtree.
      // The face owns its own phrase controller, so it is untouched by the sixty
      // rebuilds a second the flight would otherwise impose on it.
      child: _CardFace(
        key: ValueKey('deck-slot-$index'),
        card: card,
        width: width,
        height: height,
        compact: compact,
        flashIndex: _flashIndex[index],
        flashPaused: _flashPaused,
      ),
      builder: (context, child) {
        // Never launched: show it where it lands. See [_launched].
        final raw = _launched.contains(index) ? controller.value : 1.0;
        final t = Curves.easeOutCubic.transform(raw);
        final position = _flightPosition(launch, slot.topLeft, t);
        final angle = _lerp(_spins[index % _spins.length], slot.angle, t);
        final scale = _lerp(0.18, 1.0, t);
        // Hidden while its own square is open. With no wash behind the square,
        // leaving it lit would show the same phrase twice.
        final opacity =
            _openIndex == index ? 0.0 : (raw / 0.28).clamp(0.0, 1.0);

        return Positioned(
          left: position.dx,
          top: position.dy,
          width: width,
          height: height,
          child: IgnorePointer(
            // A card still in the air must not be tappable, or a fast finger
            // opens something that has not landed yet.
            ignoring: raw < 1,
            child: Opacity(
              opacity: opacity,
              child: Transform.rotate(
                angle: angle,
                child: Transform.scale(
                  scale: scale,
                  child: GestureDetector(
                    behavior: HitTestBehavior.opaque,
                    onTap: () => _openCard(index, card),
                    child: child,
                  ),
                ),
              ),
            ),
          ),
        );
      },
    );
  }
}

double _lerp(double a, double b, double t) => a + (b - a) * t;

/// The face of a card: what it is, and one line of why it is here.
class _CardFace extends StatelessWidget {
  final HomeDeckCard card;
  final double width;
  final double height;
  final bool compact;

  /// Published by the flashing line so the deck knows which phrase is on screen
  /// when this card is tapped.
  final ValueNotifier<int> flashIndex;
  final ValueListenable<bool> flashPaused;

  const _CardFace({
    super.key,
    required this.card,
    required this.width,
    required this.height,
    required this.compact,
    required this.flashIndex,
    required this.flashPaused,
  });

  Color get _tint {
    switch (card.kind) {
      case HomeDeckCardKind.ritual:
        return AppColors.accentBase;
      case HomeDeckCardKind.talk:
        return const Color(0xFFD98A7A);
      case HomeDeckCardKind.track:
        return const Color(0xFF8391C4);
      case HomeDeckCardKind.remind:
        return AppColors.premium;
      case HomeDeckCardKind.briefing:
      case HomeDeckCardKind.connect:
        return AppColors.textTertiary;
    }
  }

  /// The bottom line: when this happens, or how long it takes. Taken from the
  /// card's own option data, never invented here, so a card can't advertise a
  /// time the confirm button would not actually set.
  ({IconData icon, String text})? get _meta {
    switch (card.kind) {
      case HomeDeckCardKind.ritual:
      case HomeDeckCardKind.remind:
        final label = card.defaultOption?.displayLabel ?? '';
        return label.isEmpty
            ? null
            : (icon: Icons.schedule_rounded, text: label);
      case HomeDeckCardKind.talk:
        return (icon: Icons.chat_bubble_outline_rounded, text: '2 min chat');
      case HomeDeckCardKind.track:
        return (icon: Icons.visibility_outlined, text: 'Checks daily');
      case HomeDeckCardKind.briefing:
        return (icon: Icons.auto_stories_outlined, text: 'Ready now');
      case HomeDeckCardKind.connect:
        return (icon: Icons.link_rounded, text: '1 min setup');
    }
  }

  String get _label {
    switch (card.kind) {
      case HomeDeckCardKind.ritual:
        return 'RITUAL';
      case HomeDeckCardKind.remind:
        return 'REMINDER';
      case HomeDeckCardKind.track:
        return 'WATCHING';
      case HomeDeckCardKind.talk:
        return 'TALK';
      case HomeDeckCardKind.briefing:
        return 'BRIEFING';
      case HomeDeckCardKind.connect:
        return 'CONNECT';
    }
  }

  @override
  Widget build(BuildContext context) {
    final meta = _meta;
    return SizedBox(
      width: width,
      height: height,
      child: FauxGlassCard(
        borderRadius: 18,
        padding: const EdgeInsets.fromLTRB(15, 14, 15, 14),
        borderColor: _tint.withValues(alpha: 0.26),
        // Nearly opaque. The earlier translucent wash let the cream background
        // glare through and read as a white halo around every card.
        gradient: LinearGradient(
          begin: Alignment.topLeft,
          end: Alignment.bottomRight,
          colors: [
            Color.alphaBlend(_tint.withValues(alpha: 0.12), AppColors.surface),
            AppColors.surface,
          ],
        ),
        child: Column(
          // Centred as a whole: the kind label, the phrase and the line under it
          // read as one stack. Centring only some of them looked like a mistake.
          crossAxisAlignment: CrossAxisAlignment.center,
          children: [
            Text(
              _label,
              textAlign: TextAlign.center,
              style: TextStyle(
                fontFamily: 'GeistMono',
                fontSize: 10,
                letterSpacing: 1.0,
                fontWeight: FontWeight.w500,
                color: _tint,
              ),
            ),
            const SizedBox(height: 7),
            Expanded(
              child: FlashingPhrases(
                items: [
                  for (final s in card.phrases)
                    FlashingItem(text: s.text, iconAsset: s.iconAsset),
                ],
                index: flashIndex,
                paused: flashPaused,
                maxLines: compact ? 3 : 4,
                style: const TextStyle(
                  fontFamily: 'Outfit',
                  fontSize: 17.5,
                  height: 1.16,
                  fontWeight: FontWeight.w600,
                  color: AppColors.textPrimary,
                ),
              ),
            ),
            if (!compact && card.subtitle.isNotEmpty)
              Text(
                card.subtitle,
                maxLines: 2,
                textAlign: TextAlign.center,
                overflow: TextOverflow.ellipsis,
                style: const TextStyle(
                  fontSize: 12,
                  height: 1.32,
                  color: AppColors.textTertiary,
                ),
              ),
            if (meta != null) ...[
              const SizedBox(height: 7),
              Row(
                mainAxisSize: MainAxisSize.min,
                mainAxisAlignment: MainAxisAlignment.center,
                children: [
                  Icon(meta.icon, size: 12, color: AppColors.textSecondary),
                  const SizedBox(width: 5),
                  Flexible(
                    child: Text(
                      meta.text,
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      style: const TextStyle(
                        fontFamily: 'GeistMono',
                        fontSize: 11,
                        color: AppColors.textSecondary,
                      ),
                    ),
                  ),
                ],
              ),
            ],
          ],
        ),
      ),
    );
  }
}

