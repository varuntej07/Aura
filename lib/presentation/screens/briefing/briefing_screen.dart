import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';
import 'package:provider/provider.dart';
import 'package:url_launcher/url_launcher.dart';

import '../../../core/theme/app_colors.dart';
import '../../../core/theme/glass_card.dart';
import '../../../data/models/daily_briefing.dart';
import '../../viewmodels/briefing_viewmodel.dart';
import '../../viewmodels/notification_chat_seed.dart';
import '../../viewmodels/view_state.dart';
import '../../widgets/aura_text_field.dart';

/// Opens a briefing source article in an in-app browser. A failed launch (or an
/// unparseable url) is a silent no-op — never throw out of a tap.
Future<void> _launchSource(String url) async {
  final uri = Uri.tryParse(url);
  if (uri == null) return;
  try {
    await launchUrl(uri, mode: LaunchMode.inAppBrowserView);
  } catch (_) {
    // no-op
  }
}

/// Full-screen daily briefing. Opened from the drawer or a briefing push tap.
/// Shows Buddy's synthesized narrative plus the sources it drew on. When no
/// scheduled digest is ready (a new/cold-start user, no interest vector yet), the
/// empty state offers a "Catch me up on the world" button that fetches an on-demand
/// world snapshot. A bottom chat launcher morphs from a pill into an in-place input
/// that hands off into the full chat, seeded to talk about the briefing.
class BriefingScreen extends StatefulWidget {
  const BriefingScreen({super.key});

  @override
  State<BriefingScreen> createState() => _BriefingScreenState();
}

class _BriefingScreenState extends State<BriefingScreen> {
  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addPostFrameCallback((_) {
      context.read<BriefingViewModel>().load();
    });
  }

  @override
  Widget build(BuildContext context) {
    return AmbientBackground(
      child: Scaffold(
        backgroundColor: Colors.transparent,
        body: SafeArea(
          child: Consumer<BriefingViewModel>(
            builder: (context, vm, _) {
              final briefing = vm.briefing;
              return Stack(
                children: [
                  Column(
                    children: [
                      _Header(
                        // The refresh icon only makes sense on the on-demand world
                        // snapshot; the scheduled digest regenerates daily on its own.
                        onRefresh: vm.isWorldSnapshot
                            ? () => vm.fetchWorldNow(refresh: true)
                            : null,
                        refreshing: vm.fetchingWorld,
                      ),
                      Expanded(child: _buildContent(vm)),
                    ],
                  ),
                  if (briefing != null)
                    Positioned(
                      left: 0,
                      right: 0,
                      bottom: 0,
                      child: _ChatLauncherBar(briefing: briefing),
                    ),
                ],
              );
            },
          ),
        ),
      ),
    );
  }

  Widget _buildContent(BriefingViewModel vm) {
    if (vm.state == ViewState.loading || vm.state == ViewState.idle) {
      return const Center(
        child: SizedBox(
          width: 22,
          height: 22,
          child: CircularProgressIndicator(strokeWidth: 2),
        ),
      );
    }
    final briefing = vm.briefing;
    if (briefing != null) {
      return _BriefingBody(briefing: briefing);
    }
    return _EmptyState(
      fetching: vm.fetchingWorld,
      error: vm.worldError,
      onCatchUp: () => vm.fetchWorldNow(),
    );
  }
}

class _Header extends StatelessWidget {
  final VoidCallback? onRefresh;
  final bool refreshing;

  const _Header({this.onRefresh, this.refreshing = false});

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 20, vertical: 12),
      child: Row(
        children: [
          GlassIconButton(
            icon: Icons.arrow_back_rounded,
            onTap: () => context.pop(),
          ),
          const SizedBox(width: 14),
          const Text(
            'Daily briefing',
            style: TextStyle(
              fontFamily: 'Outfit',
              color: AppColors.textPrimary,
              fontSize: 22,
              fontWeight: FontWeight.w700,
              letterSpacing: -0.4,
            ),
          ),
          const Spacer(),
          if (refreshing)
            const SizedBox(
              width: 44,
              height: 44,
              child: Center(
                child: SizedBox(
                  width: 18,
                  height: 18,
                  child: CircularProgressIndicator(strokeWidth: 2),
                ),
              ),
            )
          else if (onRefresh != null)
            GlassIconButton(
              icon: Icons.refresh_rounded,
              onTap: onRefresh!,
            ),
        ],
      ),
    );
  }
}

class _BriefingBody extends StatelessWidget {
  final DailyBriefing briefing;
  const _BriefingBody({required this.briefing});

  @override
  Widget build(BuildContext context) {
    // The world snapshot returns discrete items (each a short blurb + an optional
    // grounded citation). The scheduled digest has only a narrative, so fall back to
    // splitting it on blank lines into items with no citation.
    final items = briefing.items.isNotEmpty
        ? briefing.items
        : briefing.narrative
            .split(RegExp(r'\n\s*\n'))
            .map((p) => p.trim())
            .where((p) => p.isNotEmpty)
            .map((p) => BriefingItem(text: p))
            .toList();

    // Number only the items that have a real, tappable source, sequentially.
    var citationCounter = 0;
    final children = <Widget>[];
    for (var i = 0; i < items.length; i++) {
      final item = items[i];
      final ci = item.citationIndex;
      final hasCitation = ci != null &&
          ci >= 0 &&
          ci < briefing.sources.length &&
          briefing.sources[ci].url.trim().isNotEmpty;
      if (i > 0) children.add(const SizedBox(height: 18));
      children.add(_NewsItem(
        text: item.text,
        citationNumber: hasCitation ? ++citationCounter : null,
        citationUrl: hasCitation ? briefing.sources[ci].url : null,
      ));
    }

    return ListView(
      padding: const EdgeInsets.fromLTRB(22, 8, 22, 0),
      children: [
        ...children,
        // Clear the floating chat launcher and the nav bar.
        SizedBox(height: MediaQuery.of(context).viewPadding.bottom + 96),
      ],
    );
  }
}

/// A single news item: a short blurb on the bare canvas (no card, no border), at a
/// smaller size, with an optional tappable superscript citation number that opens the
/// grounding source.
class _NewsItem extends StatelessWidget {
  final String text;
  final int? citationNumber;
  final String? citationUrl;

  const _NewsItem({required this.text, this.citationNumber, this.citationUrl});

  @override
  Widget build(BuildContext context) {
    return Text.rich(
      TextSpan(
        text: text,
        children: [
          if (citationNumber != null && citationUrl != null)
            WidgetSpan(
              alignment: PlaceholderAlignment.top,
              child: GestureDetector(
                onTap: () => _launchSource(citationUrl!),
                child: Padding(
                  padding: const EdgeInsets.only(left: 3),
                  child: Text(
                    '$citationNumber',
                    style: const TextStyle(
                      color: AppColors.accent,
                      fontSize: 10,
                      height: 1,
                      fontWeight: FontWeight.w700,
                    ),
                  ),
                ),
              ),
            ),
        ],
      ),
      style: const TextStyle(
        color: AppColors.textPrimary,
        fontSize: 13,
        height: 1.5,
      ),
    );
  }
}

/// Empty state shown when no scheduled briefing is ready. Instead of dead-ending a
/// new user, it offers a live "Catch me up on the world" action (the on-demand world
/// snapshot), which also bootstraps their interest profile through the follow-on chat.
class _EmptyState extends StatelessWidget {
  final bool fetching;
  final String? error;
  final VoidCallback onCatchUp;

  const _EmptyState({
    required this.fetching,
    required this.error,
    required this.onCatchUp,
  });

  @override
  Widget build(BuildContext context) {
    return Center(
      child: Padding(
        padding: const EdgeInsets.symmetric(horizontal: 40),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            const Icon(Icons.public_rounded,
                color: AppColors.textTertiary, size: 40),
            const SizedBox(height: 14),
            const Text(
              'Nothing personal yet',
              style: TextStyle(
                color: AppColors.textPrimary,
                fontSize: 17,
                fontWeight: FontWeight.w600,
              ),
            ),
            const SizedBox(height: 8),
            const Text(
              "I'm still learning what you're into. Want me to catch you up on "
              "what's going on out in the world meanwhile?",
              textAlign: TextAlign.center,
              style: TextStyle(
                color: AppColors.textTertiary,
                fontSize: 14,
                height: 1.5,
              ),
            ),
            const SizedBox(height: 22),
            _CatchUpButton(fetching: fetching, onTap: onCatchUp),
            if (error != null) ...[
              const SizedBox(height: 14),
              Text(
                error!,
                textAlign: TextAlign.center,
                style: const TextStyle(
                  color: AppColors.error,
                  fontSize: 13,
                  height: 1.4,
                ),
              ),
            ],
          ],
        ),
      ),
    );
  }
}

class _CatchUpButton extends StatelessWidget {
  final bool fetching;
  final VoidCallback onTap;

  const _CatchUpButton({required this.fetching, required this.onTap});

  @override
  Widget build(BuildContext context) {
    return GestureDetector(
      onTap: fetching ? null : onTap,
      child: Container(
        padding: const EdgeInsets.symmetric(horizontal: 22, vertical: 14),
        decoration: BoxDecoration(
          color: AppColors.accent,
          borderRadius: BorderRadius.circular(26),
          boxShadow: const [
            BoxShadow(
              color: Color(0x331EC8B0),
              blurRadius: 18,
              offset: Offset(0, 6),
            ),
          ],
        ),
        child: Row(
          mainAxisSize: MainAxisSize.min,
          children: [
            if (fetching)
              const SizedBox(
                width: 18,
                height: 18,
                child: CircularProgressIndicator(
                  strokeWidth: 2,
                  valueColor: AlwaysStoppedAnimation(Colors.white),
                ),
              )
            else
              const Icon(Icons.public_rounded, color: Colors.white, size: 20),
            const SizedBox(width: 10),
            Text(
              fetching ? 'Pulling the world together' : 'Catch me up on the world',
              style: const TextStyle(
                color: Colors.white,
                fontSize: 15,
                fontWeight: FontWeight.w600,
              ),
            ),
          ],
        ),
      ),
    );
  }
}

/// The bottom chat affordance. Collapsed, it is a small "chat" pill aligned right;
/// tapping it animates open to the LEFT into the same text field the chat screen uses
/// (`AuraTextField` + a send button, no attachment button), in place and WITHOUT
/// popping the keyboard. Sending hands off into the full chat (`/chat/new`), seeded
/// with the briefing opener and the user's typed first message, so the conversation
/// continues seamlessly. Reuses the chat stack rather than embedding a second surface.
class _ChatLauncherBar extends StatefulWidget {
  final DailyBriefing briefing;
  const _ChatLauncherBar({required this.briefing});

  @override
  State<_ChatLauncherBar> createState() => _ChatLauncherBarState();
}

class _ChatLauncherBarState extends State<_ChatLauncherBar> {
  static const double _pillWidth = 86;

  final TextEditingController _text = TextEditingController();
  final FocusNode _focus = FocusNode();
  bool _expanded = false;

  @override
  void dispose() {
    _text.dispose();
    _focus.dispose();
    super.dispose();
  }

  // Open the input in place but do NOT focus it — the keyboard stays down until the
  // user actually taps the field to type.
  void _expand() => setState(() => _expanded = true);

  void _send() {
    final typed = _text.text.trim();
    FocusScope.of(context).unfocus();
    context.push(
      '/chat/new',
      extra: NotificationChatSeed(
        origin: NotificationChatOrigin.briefing,
        openingMessage: widget.briefing.chatSeedMessage,
        firstUserMessage: typed,
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final bottomInset = MediaQuery.of(context).viewPadding.bottom;
    return Padding(
      padding: EdgeInsets.only(
        left: 20,
        right: 20,
        top: 8,
        bottom: bottomInset + 16,
      ),
      child: LayoutBuilder(
        builder: (context, constraints) {
          return Align(
            alignment: Alignment.centerRight,
            child: AnimatedSize(
              duration: const Duration(milliseconds: 260),
              curve: Curves.easeOutCubic,
              alignment: Alignment.centerRight,
              child: SizedBox(
                width: _expanded ? constraints.maxWidth : _pillWidth,
                child: _expanded ? _buildInput() : _buildPill(),
              ),
            ),
          );
        },
      ),
    );
  }

  Widget _buildPill() {
    return GestureDetector(
      onTap: _expand,
      child: Container(
        height: 46,
        decoration: BoxDecoration(
          color: AppColors.accent,
          borderRadius: BorderRadius.circular(23),
          boxShadow: const [
            BoxShadow(
              color: Color(0x331EC8B0),
              blurRadius: 16,
              offset: Offset(0, 5),
            ),
          ],
        ),
        child: const Center(
          child: Text(
            'chat',
            style: TextStyle(
              color: Colors.white,
              fontSize: 15,
              fontWeight: FontWeight.w600,
            ),
          ),
        ),
      ),
    );
  }

  // Mirrors the chat composer: the same rounded field (AuraTextField) plus a send
  // button, minus the attachment (+) button. One field, no nested glass container.
  Widget _buildInput() {
    return Row(
      crossAxisAlignment: CrossAxisAlignment.end,
      children: [
        Expanded(
          child: AuraTextField(
            controller: _text,
            focusNode: _focus,
            hint: 'Ask Buddy about this',
            onSubmitted: (_) => _send(),
          ),
        ),
        const SizedBox(width: 10),
        _SendCircle(onTap: _send),
      ],
    );
  }
}

class _SendCircle extends StatelessWidget {
  final VoidCallback onTap;
  const _SendCircle({required this.onTap});

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
        child: const Icon(Icons.arrow_upward_rounded, color: Colors.white, size: 20),
      ),
    );
  }
}
