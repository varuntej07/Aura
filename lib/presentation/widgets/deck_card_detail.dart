import 'dart:math' as math;

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:provider/provider.dart';

import '../../core/analytics/funnel_events.dart';
import '../../core/theme/app_colors.dart';
import '../../core/theme/glass_card.dart';
import '../../data/models/home_deck_card.dart';
import '../../data/services/posthog_analytics_service.dart';
import '../viewmodels/home_deck_viewmodel.dart';

/// The opened card: one large square in the middle of the screen.
///
/// It is a route rather than a layer inside the deck because the deck lives in a
/// band a few hundred pixels tall, and a square sized against that band is the
/// small card the square exists to replace. A route sees the whole screen.
///
/// The barrier is fully transparent by design. `ModalBarrier` still hit-tests at
/// zero opacity, so tap-to-close works with nothing painted over the cards
/// behind, which is the point: no wash, no dimming, no blur.
Future<void> showDeckCardDetail(
  BuildContext context, {
  required HomeDeckCard card,
  required int index,
  required int initialSuggestion,
  required void Function(String route) onNavigate,
  required void Function(String openingMessage) onStartTalk,
  required void Function(String message) onCreated,
}) {
  return showGeneralDialog<void>(
    context: context,
    useRootNavigator: true,
    barrierDismissible: true,
    barrierLabel: MaterialLocalizations.of(context).modalBarrierDismissLabel,
    barrierColor: const Color(0x00000000),
    transitionDuration: const Duration(milliseconds: 300),
    pageBuilder: (_, _, _) => DeckCardDetail(
      card: card,
      index: index,
      initialSuggestion: initialSuggestion,
      onNavigate: onNavigate,
      onStartTalk: onStartTalk,
      onCreated: onCreated,
    ),
    transitionBuilder: (_, animation, _, child) {
      final v = Curves.easeOutCubic.transform(animation.value);
      return Transform(
        alignment: Alignment.center,
        transform: Matrix4.identity()
          ..setEntry(3, 2, 0.001)
          ..rotateY((1 - v) * math.pi * 0.35),
        child: Opacity(opacity: v, child: child),
      );
    },
  );
}

class DeckCardDetail extends StatefulWidget {
  final HomeDeckCard card;
  final int index;
  final int initialSuggestion;
  final void Function(String route) onNavigate;
  final void Function(String openingMessage) onStartTalk;
  final void Function(String message) onCreated;

  const DeckCardDetail({
    super.key,
    required this.card,
    required this.index,
    required this.initialSuggestion,
    required this.onNavigate,
    required this.onStartTalk,
    required this.onCreated,
  });

  @override
  State<DeckCardDetail> createState() => _DeckCardDetailState();
}

class _DeckCardDetailState extends State<DeckCardDetail> {
  late String _suggestionId;
  String? _optionId;
  String? _error;
  bool _confirming = false;

  List<HomeDeckSuggestion> get _phrases => widget.card.phrases;

  @override
  void initState() {
    super.initState();
    // Opens on the phrase that was on the face when it was tapped, so the card
    // does not appear to change its mind under the user's finger.
    final phrases = widget.card.phrases;
    _suggestionId = phrases[widget.initialSuggestion.clamp(0, phrases.length - 1)].id;
    _optionId = widget.card.defaultOption?.id;
  }

  HomeDeckSuggestion get _suggestion => _phrases.firstWhere(
        (s) => s.id == _suggestionId,
        orElse: () => _phrases.first,
      );

  HomeDeckOption? get _option {
    final options = widget.card.options;
    if (options.isEmpty) return null;
    return options.firstWhere(
      (o) => o.id == _optionId,
      orElse: () => options.first,
    );
  }

  bool get _isNavigation =>
      widget.card.action.type == HomeDeckActionType.navigate ||
      widget.card.action.type == HomeDeckActionType.chatOpen;

  /// True once the user holds as many rituals as the server will keep. Offering a
  /// fifth here would only produce a 409 under their finger.
  bool get _atRitualCap {
    if (widget.card.action.type != HomeDeckActionType.ritualCreate) return false;
    final status = context.read<HomeDeckViewModel>().status;
    return status.ritualsMax > 0 && status.ritualsActive >= status.ritualsMax;
  }

  Future<void> _confirm() async {
    final card = widget.card;
    if (card.action.type == HomeDeckActionType.navigate) {
      Navigator.of(context).pop();
      widget.onNavigate(card.action.route);
      return;
    }
    if (card.action.type == HomeDeckActionType.chatOpen) {
      Navigator.of(context).pop();
      widget.onStartTalk(card.action.openingMessage);
      return;
    }

    final vm = context.read<HomeDeckViewModel>();
    final suggestion = _suggestion;
    setState(() {
      _confirming = true;
      _error = null;
    });
    final error = await vm.confirm(
      index: widget.index,
      card: card,
      suggestion: suggestion,
      option: _option,
    );
    if (!mounted) return;

    if (error != null) {
      // The square stays open and retryable. Closing it and only then admitting
      // the failure would have told the user it worked.
      _track(FunnelEvents.deckCardFailed);
      setState(() {
        _confirming = false;
        _error = error;
      });
      return;
    }

    _track(FunnelEvents.deckCardConfirmed);
    HapticFeedback.lightImpact();
    // The phrase is spent: the card must not keep offering something that now
    // exists.
    vm.consumeSuggestion(widget.index, suggestion.id);
    Navigator.of(context).pop();
    widget.onCreated(_confirmationFor(suggestion));
  }

  void _track(String event) {
    context.read<PostHogAnalyticsService>().trackEvent(
      event,
      properties: {FunnelEvents.propDeckCardKind: widget.card.kind.name},
    );
  }

  String _confirmationFor(HomeDeckSuggestion suggestion) {
    final when = _option?.displayLabel ?? '';
    switch (widget.card.action.type) {
      case HomeDeckActionType.ritualCreate:
        return when.isEmpty
            ? "Done. That's on me now."
            : "Done. $when, that's on me now.";
      case HomeDeckActionType.reminderCreate:
        // The label is capitalised now ("Today 3PM"), so it reads as its own
        // clause rather than being wedged mid-sentence.
        return when.isEmpty ? "Set. I'll remind you." : "Set. $when.";
      case HomeDeckActionType.trackerCreate:
        return "On it. I'll tell you when something changes.";
      default:
        return 'Done.';
    }
  }

  Color get _tint {
    switch (widget.card.kind) {
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

  String get _label {
    switch (widget.card.kind) {
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
    final media = MediaQuery.of(context);
    final available = Size(
      media.size.width - 40,
      media.size.height - media.viewPadding.vertical - 40,
    );
    // A square, capped so it stays a card on a tablet instead of becoming a page.
    final side = math.min(math.min(available.width, available.height), 380.0);

    // A dialog route has no Material ancestor of its own, and Text without one
    // renders with the framework's debug underlines. Transparent, so it adds no
    // surface of its own on top of the card's.
    return Material(
      type: MaterialType.transparency,
      child: Center(
        child: SizedBox.square(
          dimension: side,
          child: FauxGlassCard(
            borderRadius: 24,
            borderColor: _tint.withValues(alpha: 0.26),
            padding: const EdgeInsets.fromLTRB(20, 18, 20, 20),
            // Fully opaque: with no wash behind the square, a translucent fill
            // would let the other cards and the orb show through it.
            gradient: const LinearGradient(
              colors: [AppColors.surface, AppColors.surface],
            ),
            child: _buildBody(),
          ),
        ),
      ),
    );
  }

  Widget _buildBody() {
    final card = widget.card;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Row(
          children: [
            Expanded(
              child: Text(
                _label,
                style: TextStyle(
                  fontFamily: 'GeistMono',
                  fontSize: 10,
                  letterSpacing: 1.0,
                  fontWeight: FontWeight.w500,
                  color: _tint,
                ),
              ),
            ),
            GestureDetector(
              onTap: _confirming ? null : () => Navigator.of(context).pop(),
              behavior: HitTestBehavior.opaque,
              child: const Padding(
                padding: EdgeInsets.only(left: 8, bottom: 4),
                child: Icon(Icons.close_rounded,
                    size: 20, color: AppColors.textTertiary),
              ),
            ),
          ],
        ),
        const SizedBox(height: 10),
        if (_atRitualCap) ..._capBody() else ..._offerBody(card),
      ],
    );
  }

  /// At the cap the only honest thing left to offer is the rituals they already
  /// have, so the square becomes that instead of a dead end.
  List<Widget> _capBody() {
    final status = context.read<HomeDeckViewModel>().status;
    return [
      Text(
        'You have ${status.ritualsActive} rituals going',
        style: const TextStyle(
          fontFamily: 'Outfit',
          fontSize: 22,
          height: 1.15,
          fontWeight: FontWeight.w600,
          color: AppColors.textPrimary,
        ),
      ),
      const SizedBox(height: 8),
      const Text(
        "That's as many as I can keep for you. Stop one and I'll pick up another.",
        style: TextStyle(
          fontSize: 13,
          height: 1.35,
          color: AppColors.textSecondary,
        ),
      ),
      const Spacer(),
      _button('Manage them', () {
        Navigator.of(context).pop();
        widget.onNavigate('/reminders');
      }),
    ];
  }

  List<Widget> _offerBody(HomeDeckCard card) {
    if (_isNavigation) {
      return [
        Text(
          card.title,
          style: const TextStyle(
            fontFamily: 'Outfit',
            fontSize: 24,
            height: 1.15,
            fontWeight: FontWeight.w600,
            color: AppColors.textPrimary,
          ),
        ),
        if (card.subtitle.isNotEmpty) ...[
          const SizedBox(height: 10),
          Text(
            card.subtitle,
            style: const TextStyle(
              fontSize: 13.5,
              height: 1.4,
              color: AppColors.textSecondary,
            ),
          ),
        ],
        const Spacer(),
        _button(card.primaryLabel.isEmpty ? "Let's do it" : card.primaryLabel,
            _confirm),
      ];
    }

    return [
      // No subtitle here. It is written for one phrase and this square lists
      // several, so it reads as a caption for the wrong line ("A push when I
      // need it" under "Something small to wake up to"). The phrases speak for
      // themselves. The navigation squares above keep theirs: they have no
      // phrases for it to contradict.
      // The phrases, all of them, so the one that was flashing is a starting
      // point rather than the only thing on offer. Expanded rather than Flexible:
      // it takes the middle of the square and pushes the chips and the button to
      // the bottom edge, instead of leaving the lower half of a square empty.
      Expanded(
        child: SingleChildScrollView(
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              for (final suggestion in _phrases)
                _PhraseRow(
                  text: suggestion.text,
                  selected: suggestion.id == _suggestionId,
                  tint: _tint,
                  onTap: _confirming
                      ? null
                      : () => setState(() => _suggestionId = suggestion.id),
                ),
            ],
          ),
        ),
      ),
      if (card.options.isNotEmpty) ...[
        const SizedBox(height: 12),
        Wrap(
          spacing: 6,
          runSpacing: 6,
          children: [
            for (final option in card.options)
              _OptionChip(
                label: option.displayLabel,
                selected: option.id == (_option?.id ?? _optionId),
                onTap: _confirming
                    ? null
                    : () => setState(() => _optionId = option.id),
              ),
          ],
        ),
      ],
      if (_error != null) ...[
        const SizedBox(height: 10),
        Text(
          _error!,
          style: const TextStyle(
            fontSize: 12.5,
            height: 1.3,
            color: AppColors.error,
          ),
        ),
      ],
      const SizedBox(height: 14),
      _button(
        card.primaryLabel.isEmpty ? "Let's do it" : card.primaryLabel,
        _confirm,
      ),
    ];
  }

  Widget _button(String label, VoidCallback onTap) {
    return GestureDetector(
      onTap: _confirming ? null : onTap,
      behavior: HitTestBehavior.opaque,
      child: Container(
        height: 48,
        width: double.infinity,
        alignment: Alignment.center,
        decoration: BoxDecoration(
          color: AppColors.accent.withValues(alpha: 0.18),
          borderRadius: BorderRadius.circular(30),
          border: Border.all(color: AppColors.accent.withValues(alpha: 0.38)),
        ),
        child: _confirming
            ? const SizedBox(
                width: 18,
                height: 18,
                child: CircularProgressIndicator(
                  strokeWidth: 2,
                  valueColor: AlwaysStoppedAnimation<Color>(AppColors.accentBase),
                ),
              )
            : Text(
                label,
                style: TextStyle(
                  fontFamily: 'Outfit',
                  fontSize: 15,
                  fontWeight: FontWeight.w600,
                  color: AppColors.accentDark,
                ),
              ),
      ),
    );
  }
}

/// One phrase in the opened square, selectable.
class _PhraseRow extends StatelessWidget {
  final String text;
  final bool selected;
  final Color tint;
  final VoidCallback? onTap;

  const _PhraseRow({
    required this.text,
    required this.selected,
    required this.tint,
    this.onTap,
  });

  @override
  Widget build(BuildContext context) {
    return GestureDetector(
      onTap: onTap,
      behavior: HitTestBehavior.opaque,
      child: AnimatedContainer(
        duration: const Duration(milliseconds: 200),
        curve: Curves.easeOutCubic,
        margin: const EdgeInsets.only(bottom: 6),
        padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 11),
        decoration: BoxDecoration(
          color: selected
              ? tint.withValues(alpha: 0.14)
              : AppColors.glassWhiteFill,
          borderRadius: BorderRadius.circular(30),
          border: Border.all(
            color: selected
                ? tint.withValues(alpha: 0.42)
                : AppColors.glassBorderLight,
          ),
        ),
        child: Row(
          children: [
            Expanded(
              child: Text(
                text,
                style: TextStyle(
                  fontFamily: 'Outfit',
                  fontSize: 15,
                  height: 1.2,
                  fontWeight: selected ? FontWeight.w600 : FontWeight.w500,
                  color: AppColors.textPrimary,
                ),
              ),
            ),
            if (selected)
              Icon(Icons.check_rounded, size: 16, color: tint),
          ],
        ),
      ),
    );
  }
}

/// A time or cadence choice. Unchanged in behaviour from the old back face.
class _OptionChip extends StatelessWidget {
  final String label;
  final bool selected;
  final VoidCallback? onTap;

  const _OptionChip({
    required this.label,
    required this.selected,
    this.onTap,
  });

  @override
  Widget build(BuildContext context) {
    return GestureDetector(
      onTap: onTap,
      behavior: HitTestBehavior.opaque,
      child: AnimatedContainer(
        duration: const Duration(milliseconds: 200),
        curve: Curves.easeOutCubic,
        padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 9),
        decoration: BoxDecoration(
          color: selected ? AppColors.textPrimary : AppColors.glassWhiteFill,
          borderRadius: BorderRadius.circular(30),
          border: Border.all(
            color: selected ? AppColors.textPrimary : AppColors.glassBorderLight,
          ),
        ),
        child: Text(
          label,
          style: TextStyle(
            fontSize: 12.5,
            fontWeight: FontWeight.w500,
            color: selected ? AppColors.surface : AppColors.textSecondary,
          ),
        ),
      ),
    );
  }
}
