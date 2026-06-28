import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';
import 'package:provider/provider.dart';
import 'package:url_launcher/url_launcher.dart';

import '../../../core/theme/app_colors.dart';
import '../../../core/theme/glass_card.dart';
import '../../../data/models/daily_briefing.dart';
import '../../viewmodels/auth_viewmodel.dart';
import '../../viewmodels/briefing_viewmodel.dart';
import '../../viewmodels/text_chat_viewmodel.dart';
import '../../widgets/chat_message_list.dart';
import '../../widgets/message_input.dart';
import '../../widgets/sign_in_gate_dialog.dart';
import '../reminders/reminders_screen.dart';

/// A muted "Yesterday" / "From M/D" label when the served briefing is a prior day's
/// (the fallback). Null for today's briefing and the world snapshot (empty date).
String? _priorDayLabel(String isoDate) {
  if (isoDate.isEmpty) return null;
  final parsed = DateTime.tryParse(isoDate);
  if (parsed == null) return null;
  final now = DateTime.now();
  final today = DateTime(now.year, now.month, now.day);
  final that = DateTime(parsed.year, parsed.month, parsed.day);
  final diff = today.difference(that).inDays;
  if (diff <= 0) return null;
  if (diff == 1) return 'Yesterday';
  return 'From ${that.month}/${that.day}';
}

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
/// Shows the synthesized news with an always-visible composer that turns the screen
/// into a live chat about the news IN PLACE (no navigation): the first send seeds the
/// briefing as the opening chat bubble (see [ChatViewModel.startBriefingConversation]),
/// then the conversation continues below it and is saved as a normal Buddy session, so
/// it lands in chat history. When no scheduled digest is ready (a new/cold-start user),
/// the empty state offers a "Catch me up on the world" snapshot instead.
class BriefingScreen extends StatefulWidget {
  const BriefingScreen({super.key});

  @override
  State<BriefingScreen> createState() => _BriefingScreenState();
}

class _BriefingScreenState extends State<BriefingScreen> {
  final _scrollController = ScrollController();

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addPostFrameCallback((_) async {
      if (!mounted) return;
      context.read<BriefingViewModel>().load();
      // The embedded chat shares the main Buddy session lifecycle. init opens an empty
      // session, so the news reader shows until the first send seeds the conversation.
      final uid = context.read<AuthViewModel>().user?.uid;
      await context.read<TextChatViewModel>().init(uid);
    });
  }

  @override
  void dispose() {
    _scrollController.dispose();
    super.dispose();
  }

  // Animated — used after a send so the new turn slides into view.
  void _scrollToBottom() {
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (_scrollController.hasClients) {
        _scrollController.animateTo(
          _scrollController.position.maxScrollExtent,
          duration: const Duration(milliseconds: 250),
          curve: Curves.easeOut,
        );
      }
    });
  }

  // First send seeds the briefing as the opening bubble; later sends are ordinary turns.
  void _handleSend(DailyBriefing briefing, TextChatViewModel chatVm, String text) {
    final authViewModel = context.read<AuthViewModel>();
    final user = authViewModel.user;
    if (user == null) {
      showSignInGateDialog(context, authViewModel: authViewModel);
      return;
    }
    final chatStarted = chatVm.messages.isNotEmpty || chatVm.isStreaming;
    if (chatStarted) {
      chatVm.sendMessage(text, user.uid);
    } else {
      chatVm.startBriefingConversation(
        newsMessage: _briefingAsMarkdown(briefing),
        firstUserMessage: text,
      );
    }
    _scrollToBottom();
  }

  @override
  Widget build(BuildContext context) {
    return AmbientBackground(
      child: Scaffold(
        backgroundColor: Colors.transparent,
        resizeToAvoidBottomInset: true,
        body: SafeArea(
          child: Consumer2<BriefingViewModel, TextChatViewModel>(
            builder: (context, briefingVm, chatVm, _) {
              final briefing = briefingVm.briefing;
              return Column(
                children: [
                  const _Header(),
                  Expanded(child: _buildBody(briefingVm, chatVm)),
                  if (briefing != null)
                    MessageInput(
                      isLoading: chatVm.state == ViewState.loading,
                      hint: 'Ask Buddy about this',
                      allowAttachments: false,
                      onSend: (text, _) => _handleSend(briefing, chatVm, text),
                      onStop: chatVm.stopGeneration,
                      extraBottomPadding: 16,
                    ),
                ],
              );
            },
          ),
        ),
      ),
    );
  }

  Widget _buildBody(BriefingViewModel briefingVm, TextChatViewModel chatVm) {
    if (briefingVm.state == ViewState.loading || briefingVm.state == ViewState.idle) {
      return const _BriefingSkeleton();
    }
    final briefing = briefingVm.briefing;
    if (briefing == null) {
      return _EmptyState(
        fetching: briefingVm.fetchingWorld,
        error: briefingVm.worldError,
        onCatchUp: () => briefingVm.fetchWorldNow(),
      );
    }
    // News present: the reader until the user starts chatting, then the live chat in
    // place. The news is the first chat bubble, seeded by the first send.
    final chatStarted = chatVm.messages.isNotEmpty || chatVm.isStreaming;
    if (!chatStarted) {
      return _BriefingBody(briefing: briefing);
    }
    return ChatMessageList(
      messages: chatVm.messages,
      scrollController: _scrollController,
      isStreaming: chatVm.isStreaming,
      streamingOutput: chatVm.streamingOutput,
      onRetry: chatVm.retryLastMessage,
      onEdit: chatVm.editAndResend,
      onFeedback: chatVm.setFeedback,
      onViewReminders: () => Navigator.push(context, RemindersScreen.route()),
      onClarificationSubmit: chatVm.submitClarification,
    );
  }
}

/// Renders the briefing into one readable assistant message (the opening chat bubble):
/// a short lead-in, then each item as a bullet with its grounded source as a tappable
/// markdown link. Falls back to splitting the narrative into paragraphs when there are
/// no discrete items (the scheduled digest) — the same fallback [_BriefingBody] uses.
String _briefingAsMarkdown(DailyBriefing briefing) {
  final items = briefing.items.isNotEmpty
      ? briefing.items
      : briefing.narrative
          .split(RegExp(r'\n\s*\n'))
          .map((p) => p.trim())
          .where((p) => p.isNotEmpty)
          .map((p) => BriefingItem(text: p))
          .toList();

  final buffer = StringBuffer("Here's what's in today's briefing:\n");
  for (final item in items) {
    buffer.write('\n- ${item.text}');
    final ci = item.citationIndex;
    if (ci != null && ci >= 0 && ci < briefing.sources.length) {
      final url = briefing.sources[ci].url.trim();
      if (url.isNotEmpty) buffer.write(' ([source]($url))');
    }
  }
  return buffer.toString();
}

class _Header extends StatelessWidget {
  const _Header();

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
        category: item.category,
        citationNumber: hasCitation ? ++citationCounter : null,
        citationUrl: hasCitation ? briefing.sources[ci].url : null,
      ));
    }

    final priorDayLabel = _priorDayLabel(briefing.date);

    return ListView(
      padding: const EdgeInsets.fromLTRB(22, 8, 22, 0),
      children: [
        if (priorDayLabel != null) ...[
          Text(
            priorDayLabel,
            style: const TextStyle(
              color: AppColors.textTertiary,
              fontSize: 12,
              fontWeight: FontWeight.w600,
              letterSpacing: 0.3,
            ),
          ),
          const SizedBox(height: 14),
        ],
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
  final String category;
  final int? citationNumber;
  final String? citationUrl;

  const _NewsItem({
    required this.text,
    this.category = '',
    this.citationNumber,
    this.citationUrl,
  });

  @override
  Widget build(BuildContext context) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        if (category.isNotEmpty) ...[
          _CategoryChip(label: category),
          const SizedBox(height: 6),
        ],
        Text.rich(
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
        ),
      ],
    );
  }
}

/// Small teal category tag above a news blurb, so the 3-4 categories are scannable.
class _CategoryChip extends StatelessWidget {
  final String label;
  const _CategoryChip({required this.label});

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
      decoration: BoxDecoration(
        color: AppColors.accent.withValues(alpha: 0.12),
        borderRadius: BorderRadius.circular(8),
      ),
      child: Text(
        label.toUpperCase(),
        style: const TextStyle(
          color: AppColors.accent,
          fontSize: 10,
          fontWeight: FontWeight.w700,
          letterSpacing: 0.5,
        ),
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

/// Loading placeholder shaped like the briefing list (category chip + blurb lines per
/// item) with a gentle pulse, so the wait reads as "content is coming" rather than a
/// bare spinner.
class _BriefingSkeleton extends StatefulWidget {
  const _BriefingSkeleton();

  @override
  State<_BriefingSkeleton> createState() => _BriefingSkeletonState();
}

class _BriefingSkeletonState extends State<_BriefingSkeleton>
    with SingleTickerProviderStateMixin {
  late final AnimationController _controller = AnimationController(
    vsync: this,
    duration: const Duration(milliseconds: 1100),
  )..repeat(reverse: true);

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return AnimatedBuilder(
      animation: _controller,
      builder: (context, _) {
        final alpha = 0.06 + 0.08 * _controller.value;
        return ListView(
          padding: const EdgeInsets.fromLTRB(22, 8, 22, 0),
          physics: const NeverScrollableScrollPhysics(),
          children: [
            for (var i = 0; i < 5; i++) ...[
              if (i > 0) const SizedBox(height: 22),
              _SkeletonItem(alpha: alpha),
            ],
          ],
        );
      },
    );
  }
}

class _SkeletonItem extends StatelessWidget {
  final double alpha;
  const _SkeletonItem({required this.alpha});

  Widget _bar(double width, double height) => Container(
        width: width,
        height: height,
        decoration: BoxDecoration(
          color: AppColors.textPrimary.withValues(alpha: alpha),
          borderRadius: BorderRadius.circular(6),
        ),
      );

  @override
  Widget build(BuildContext context) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        _bar(58, 14),
        const SizedBox(height: 10),
        _bar(double.infinity, 11),
        const SizedBox(height: 7),
        _bar(double.infinity, 11),
        const SizedBox(height: 7),
        FractionallySizedBox(
          widthFactor: 0.55,
          alignment: Alignment.centerLeft,
          child: _bar(double.infinity, 11),
        ),
      ],
    );
  }
}
