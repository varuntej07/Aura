import 'dart:async';

import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';
import 'package:provider/provider.dart';
import 'package:url_launcher/url_launcher.dart';

import '../../../core/theme/app_colors.dart';
import '../../../core/theme/glass_card.dart';
import '../../../data/local/app_database.dart';
import '../../../data/models/voice_models.dart';
import '../../../data/repositories/agent_suggestion_pills_repository.dart';
import '../../../data/repositories/chat_repository.dart';
import '../../../data/services/buddy_pills_refresher.dart';
import '../../../data/services/chat_service_provider.dart';
import '../../../data/services/chat_backup_service.dart';
import '../../../data/services/chat_session_manager.dart';
import '../../../data/services/feedback_service.dart';
import '../../../data/services/posthog_analytics_service.dart';
import '../../../core/network/connectivity_service.dart';
import '../../viewmodels/auth_viewmodel.dart';
import '../../viewmodels/home_viewmodel.dart';
import '../../viewmodels/notification_chat_seed.dart';
import '../../viewmodels/text_chat_viewmodel.dart';
import '../chat/embedded_chat_panel.dart';
import '../settings/aura_profile_screen.dart';
import '../settings/settings_screen.dart';
import '../../widgets/voice_sphere.dart';

enum _HomeMode { voice, chat }

class HomeScreen extends StatefulWidget {
  const HomeScreen({super.key});

  @override
  State<HomeScreen> createState() => _HomeScreenState();
}

class _HomeScreenState extends State<HomeScreen> with TickerProviderStateMixin {
  final _scaffoldKey = GlobalKey<ScaffoldState>();
  late final AnimationController _breathController;
  late final Animation<double> _breathAnimation;
  late final AnimationController _rippleController;
  late final Animation<double> _rippleAnimation;
  late final PageController _pageController;
  late final TextChatViewModel _textChatViewModel;
  bool _textChatViewModelCreated = false;
  _HomeMode _mode = _HomeMode.voice;

  @override
  void initState() {
    super.initState();

    _breathController = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 2000),
    )..repeat(reverse: true);
    _breathAnimation = Tween<double>(begin: 1.0, end: 1.06).animate(
      CurvedAnimation(parent: _breathController, curve: Curves.easeInOut),
    );

    _rippleController = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 900),
    )..repeat();
    _rippleAnimation = Tween<double>(begin: 1.0, end: 1.5).animate(
      CurvedAnimation(parent: _rippleController, curve: Curves.easeOut),
    );
    _pageController = PageController();

    WidgetsBinding.instance.addPostFrameCallback((_) async {
      final uid = context.read<AuthViewModel>().user?.uid;
      final vm = context.read<HomeViewModel>();

      vm.onEngagementTap = (payload) {
        context.push(
          '/chat/new',
          extra: NotificationChatSeed(
            origin: NotificationChatOrigin.engagement,
            openingMessage: payload.initialMessage,
            engagementId: payload.engagementId,
            agentContext: payload.agentContext,
          ),
        );
      };

      vm.onAgentNudgeTap = (payload) {
        context.push(
          '/agents/${payload.agentId}',
          extra: payload.chatOpener.isNotEmpty ? payload.chatOpener : null,
        );
      };

      vm.onSignalNotificationTap = (payload) async {
        // "read" content opens the source article in an in-app browser; opening
        // (not endorsing) trains the vector mildly and fires the read-path funnel
        // terminal. Anything else (or a launch failure) opens chat with Buddy.
        final wantsRead =
            payload.contentKind == 'read' && payload.url.isNotEmpty;
        if (wantsRead) {
          final uri = Uri.tryParse(payload.url);
          if (uri != null) {
            unawaited(vm.reportSignalContentOpened(payload));
            try {
              final ok = await launchUrl(uri, mode: LaunchMode.inAppBrowserView);
              if (ok) return;
            } catch (_) {
              // fall through to chat below
            }
          }
        }
        if (!mounted) return;
        context.push(
          '/chat/new',
          extra: NotificationChatSeed(
            origin: NotificationChatOrigin.signal,
            openingMessage: payload.openingChatMessage,
            notificationId: payload.notificationId,
            contentId: payload.contentId,
            category: payload.category,
          ),
        );
      };

      vm.onThreadFollowUpTap = (payload) {
        context.push(
          '/chat/new',
          extra: NotificationChatSeed(
            origin: NotificationChatOrigin.thread,
            openingMessage: payload.question,
            threadId: payload.threadId,
            suggestedReplies: payload.suggestedReplies,
          ),
        );
      };

      vm.onIcebreakerTap = (payload) {
        // An icebreaker always opens chat seeded with Buddy's opener. The
        // notification id rides along so the chat surface can attribute the
        // session + first reply back to this opener in the funnel.
        context.push(
          '/chat/new',
          extra: NotificationChatSeed(
            origin: NotificationChatOrigin.icebreaker,
            openingMessage: payload.openingChatMessage,
            notificationId: payload.notificationId,
          ),
        );
      };

      vm.onDailyBriefingTap = (_) {
        // A briefing tap opens the briefing screen
        context.push('/briefing');
      };

      vm.onTrackerUpdateTap = (payload) {
        // A topic-tracker live update always opens chat seeded with Buddy's update.
        context.push(
          '/chat/new',
          extra: NotificationChatSeed(
            origin: NotificationChatOrigin.tracker,
            openingMessage: payload.openingChatMessage,
          ),
        );
      };

      if (uid != null && uid.isNotEmpty) {
        // Warm the voice stack before the user taps the mic. Fire-and-forget so
        // it never blocks wake-word init or the first frame.
        unawaited(vm.prewarmVoice());
        await vm.initWakeWord(uid);
      }
    });
  }

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    if (!_textChatViewModelCreated) {
      _textChatViewModel = TextChatViewModel(
        backendService: context.read<ChatServiceProvider>(),
        chatRepository: context.read<ChatRepository>(),
        chatBackupService: context.read<ChatBackupService>(),
        feedbackService: context.read<FeedbackService>(),
        connectivityService: context.read<ConnectivityService>(),
        chatSessionManager: context.read<ChatSessionManager>(),
        postHogAnalyticsService: context.read<PostHogAnalyticsService>(),
        suggestionPillsRepository: context.read<AgentSuggestionPillsRepository>(),
        buddyPillsRefresher: context.read<BuddyPillsRefresher>(),
      );
      _textChatViewModelCreated = true;
    }
  }

  @override
  void dispose() {
    _breathController.dispose();
    _rippleController.dispose();
    _pageController.dispose();
    _textChatViewModel.dispose();
    super.dispose();
  }

  Future<void> _handleMicTap() async {
    final authVm = context.read<AuthViewModel>();
    if (authVm.user == null) {
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(
          content: const Text('Sign in to use voice'),
          action: SnackBarAction(
            label: 'Sign In',
            onPressed: () => context.go('/login'),
          ),
          behavior: SnackBarBehavior.floating,
          duration: const Duration(seconds: 4),
        ),
      );
      return;
    }

    final vm = context.read<HomeViewModel>();
    if (vm.hasActiveSession) {
      await vm.endSession();
    } else {
      await vm.startSession(authVm.user!.uid);
    }
  }

  void _setMode(_HomeMode mode) {
    if (_mode == mode) return;
    // Leaving the text panel for voice: drop the keyboard so it never lingers
    // over the voice screen.
    if (mode == _HomeMode.voice) FocusScope.of(context).unfocus();
    setState(() => _mode = mode);
    _pageController.animateToPage(
      mode.index,
      duration: const Duration(milliseconds: 360),
      curve: Curves.easeOutCubic,
    );
  }

  void _handleHorizontalSwipe(DragEndDetails details) {
    final velocity = details.primaryVelocity ?? 0;
    if (velocity.abs() < 200) return;     // ignore slow / ambiguous drags
    final swipingRight = velocity > 0;
    if (swipingRight) {
      // Rightward = pull the left drawer in. On Voice that opens history; 
      // on Text it steps back to Voice.
      if (_mode == _HomeMode.voice) {
        FocusScope.of(context).unfocus();
        _scaffoldKey.currentState?.openDrawer();
      } else {
        _setMode(_HomeMode.voice);
      }
    } else if (_mode == _HomeMode.voice) {
      // Leftward = advance to Text (no-op when already there).
      _setMode(_HomeMode.chat);
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      key: _scaffoldKey,
      backgroundColor: Colors.transparent,
      resizeToAvoidBottomInset: false,
      drawerScrimColor: AppColors.textPrimary.withValues(alpha: 0.28),
      // The Scaffold's own edge-swipe is disabled: on phones with gesture
      // navigation it fights the OS back-swipe.
      drawerEnableOpenDragGesture: false,
      // Pause the ambient orb animations while the history drawer is open: the
      // voice panel keeps repainting at 60fps behind the scrim and would steal
      // frame budget from the drawer's scroll. Resumed on close.
      onDrawerChanged: (isOpen) {
        if (isOpen) {
          FocusScope.of(context).unfocus();      // Drops the keyboard so opening the drawer
          _breathController.stop();
          _rippleController.stop();
        } else {
          _breathController.repeat(reverse: true);
          _rippleController.repeat();
        }
      },
      drawer: _ChatDrawer(
        onNewChat: () {
          Navigator.of(context).pop();
          context.push('/chat/new');
        },
        onSelectSession: (sessionId) {
          Navigator.of(context).pop();
          context.push('/chat/$sessionId');
        },
      ),
      body: SafeArea(
        bottom: false,
        child: Column(
          children: [
            Padding(
              padding: const EdgeInsets.symmetric(horizontal: 20, vertical: 12),
              child: Row(
                children: [
                  GlassIconButton(
                    icon: Icons.menu_rounded,
                    onTap: () => _scaffoldKey.currentState?.openDrawer(),
                  ),
                  const SizedBox(width: 14),
                  Expanded(
                    child: Center(
                      child: _HomeModeSwitch(
                        mode: _mode,
                        onChanged: _setMode,
                      ),
                    ),
                  ),
                  const SizedBox(width: 14),
                  // Settings moved into the drawer; this spacer (same width as a
                  // GlassIconButton) keeps the Voice/Text switch centered.
                  const SizedBox(width: 44),
                ],
              ),
            ),
            Expanded(
              // The pager itself no longer handles drags (NeverScrollable); the
              // wrapping detector owns horizontal swipes so they also open the
              // drawer (see [_handleHorizontalSwipe]). Taps and vertical scrolls
              // inside the pages pass through untouched.
              child: GestureDetector(
                onHorizontalDragEnd: _handleHorizontalSwipe,
                child: PageView(
                  controller: _pageController,
                  physics: const NeverScrollableScrollPhysics(),
                  onPageChanged: (index) {
                    setState(() => _mode = _HomeMode.values[index]);
                  },
                  children: [
                    _VoicePanel(
                      breathAnimation: _breathAnimation,
                      rippleAnimation: _rippleAnimation,
                      onMicTap: _handleMicTap,
                    ),
                    ChangeNotifierProvider.value(
                      value: _textChatViewModel,
                      child: const EmbeddedChatPanel(),
                    ),
                  ],
                ),
              ),
            ),
          ],
        ),
      ),
    );
  }
}

class _HomeModeSwitch extends StatelessWidget {
  final _HomeMode mode;
  final ValueChanged<_HomeMode> onChanged;

  const _HomeModeSwitch({
    required this.mode,
    required this.onChanged,
  });

  @override
  Widget build(BuildContext context) {
    return SizedBox(
      width: 176,
      child: FauxGlassCard(
        borderRadius: 24,
        padding: const EdgeInsets.all(4),
        borderColor: AppColors.glassBorderDim,
        child: Row(
          children: [
            _HomeModeButton(
              label: 'Voice',
              selected: mode == _HomeMode.voice,
              onTap: () => onChanged(_HomeMode.voice),
            ),
            _HomeModeButton(
              label: 'Text',
              selected: mode == _HomeMode.chat,
              onTap: () => onChanged(_HomeMode.chat),
            ),
          ],
        ),
      ),
    );
  }
}

class _HomeModeButton extends StatelessWidget {
  final String label;
  final bool selected;
  final VoidCallback onTap;

  const _HomeModeButton({
    required this.label,
    required this.selected,
    required this.onTap,
  });

  @override
  Widget build(BuildContext context) {
    return Expanded(
      child: GestureDetector(
        behavior: HitTestBehavior.opaque,
        onTap: onTap,
        child: AnimatedContainer(
          duration: const Duration(milliseconds: 240),
          curve: Curves.easeOutCubic,
          height: 38,
          decoration: BoxDecoration(
            color: selected
                ? AppColors.accent.withValues(alpha: 0.18)
                : Colors.transparent,
            borderRadius: BorderRadius.circular(18),
            border: selected
                ? Border.all(
                    color: AppColors.accent.withValues(alpha: 0.35),
                    width: 1,
                  )
                : null,
          ),
          child: Center(
            child: AnimatedDefaultTextStyle(
              duration: const Duration(milliseconds: 200),
              style: TextStyle(
                color: selected ? AppColors.accentDark : AppColors.textTertiary,
                fontSize: 13,
                fontWeight: selected ? FontWeight.w700 : FontWeight.w500,
              ),
              child: Text(label),
            ),
          ),
        ),
      ),
    );
  }
}

class _VoicePanel extends StatelessWidget {
  final Animation<double> breathAnimation;
  final Animation<double> rippleAnimation;
  final VoidCallback onMicTap;

  const _VoicePanel({
    required this.breathAnimation,
    required this.rippleAnimation,
    required this.onMicTap,
  });

  @override
  Widget build(BuildContext context) {
    final bottomReserve = MediaQuery.of(context).viewPadding.bottom + 48;

    return Consumer<HomeViewModel>(
      builder: (context, vm, _) {
        final endedSummary = vm.endedSummary;
        return Stack(
          children: [
            Positioned(
              top: 40,
              left: 0,
              right: 0,
              bottom: bottomReserve + 132,
              child: Align(
                alignment: Alignment.topCenter,
                child: SingleChildScrollView(
                  padding: const EdgeInsets.fromLTRB(20, 2, 20, 12),
                  child: _VoiceStatusCard(vm: vm),
                ),
              ),
            ),
            // "Voice chat ended" rating card, pinned on top
            if (endedSummary != null)
              Positioned(
                top: 8,
                left: 24,
                right: 24,
                child: _VoiceEndedCard(
                  key: ValueKey(endedSummary.sessionId ?? endedSummary.duration.inSeconds),
                  summary: endedSummary,
                  onLike: () => _showVoiceFeedbackDialog(context, vm, liked: true),
                  onDislike: () => _showVoiceFeedbackDialog(context, vm, liked: false),
                  onAutoDismiss: vm.dismissEndedSummary,
                ),
              ),
            Positioned(
              left: 0,
              right: 0,
              bottom: bottomReserve,
              child: Center(
                child: _VoiceButton(
                  micState: vm.micState,
                  voiceStatus: vm.voiceStatus,
                  breathAnimation: breathAnimation,
                  rippleAnimation: rippleAnimation,
                  onTap: onMicTap,
                ),
              ),
            ),
          ],
        );
      },
    );
  }
}

// Voice button

class _VoiceButton extends StatefulWidget {
  final MicState micState;
  final VoiceSessionStatus voiceStatus;
  final Animation<double> breathAnimation;
  final Animation<double> rippleAnimation;
  final VoidCallback onTap;

  const _VoiceButton({
    required this.micState,
    required this.voiceStatus,
    required this.breathAnimation,
    required this.rippleAnimation,
    required this.onTap,
  });

  @override
  State<_VoiceButton> createState() => _VoiceButtonState();
}

class _VoiceButtonState extends State<_VoiceButton> {
  int _labelVersion = 0;
  late String _prevLabel;

  @override
  void initState() {
    super.initState();
    _prevLabel = _labelOf(widget.voiceStatus);
  }

  @override
  void didUpdateWidget(covariant _VoiceButton oldWidget) {
    super.didUpdateWidget(oldWidget);
    final newLabel = _labelOf(widget.voiceStatus);
    if (newLabel != _prevLabel) {
      _labelVersion++;
      _prevLabel = newLabel;
    }
  }

  String _labelOf(VoiceSessionStatus status) => switch (status) {
        VoiceSessionStatus.connecting => 'Connecting...',
        VoiceSessionStatus.ready => 'Listening',
        VoiceSessionStatus.listening => 'Listening',
        VoiceSessionStatus.processing => 'Thinking',
        VoiceSessionStatus.speaking => 'Speaking',
        _ => 'Start voice',
      };

  // Sphere brightness/swirl-speed per mic state: idle is calm, listening is the
  // brightest, thinking sits in between.
  double _intensityOf(MicState state) => switch (state) {
        MicState.idle => 0.35,
        MicState.listening => 1.0,
        MicState.processing => 0.7,
      };

  @override
  Widget build(BuildContext context) {
    final label = _labelOf(widget.voiceStatus);
    return GestureDetector(
      onTap: widget.onTap,
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          AnimatedBuilder(
            animation: Listenable.merge([widget.breathAnimation, widget.rippleAnimation]),
            builder: (_, _) {
              final isActive = widget.micState != MicState.idle;
              // Idle: slow gentle breath. Active: noticeably larger + a fast pulse.
              final double scale;
              if (isActive) {
                final pulse = (widget.rippleAnimation.value - 1.0) / 0.5; // 0..1, ~900ms
                scale = 1.15 + 0.15 * pulse;
              } else {
                scale = widget.breathAnimation.value;
              }
              return Transform.scale(
                scale: scale,
                // Glowing sphere — the speech orb
                child: VoiceSphere(
                  intensity: _intensityOf(widget.micState),
                  size: 104,
                ),
              );
            },
          ),
          const SizedBox(height: 4),
          AnimatedSwitcher(
            duration: const Duration(milliseconds: 200),
            child: Text(
              label,
              key: ValueKey<String>('$label:$_labelVersion'),
              style: const TextStyle(
                color: AppColors.textTertiary,
                fontSize: 14,
              ),
            ),
          ),
        ],
      ),
    );
  }
}

// Voice status card

class _VoiceStatusCard extends StatefulWidget {
  final HomeViewModel vm;
  const _VoiceStatusCard({required this.vm});

  @override
  State<_VoiceStatusCard> createState() => _VoiceStatusCardState();
}

class _VoiceStatusCardState extends State<_VoiceStatusCard> {
  final _scrollController = ScrollController();

  @override
  void didUpdateWidget(covariant _VoiceStatusCard oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (widget.vm.voiceTranscript.length != oldWidget.vm.voiceTranscript.length ||
        widget.vm.liveTranscript != oldWidget.vm.liveTranscript) {
      WidgetsBinding.instance.addPostFrameCallback((_) {
        if (!_scrollController.hasClients) return;
        _scrollController.animateTo(
          _scrollController.position.maxScrollExtent,
          duration: const Duration(milliseconds: 260),
          curve: Curves.easeOutCubic,
        );
      });
    }
  }

  @override
  void dispose() {
    _scrollController.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final entries = widget.vm.voiceTranscript;
    final hasTranscript = entries.isNotEmpty;

    if (widget.vm.voiceStatus == VoiceSessionStatus.error) {
      final msg = widget.vm.error?.message ?? "Something went sideways with the call. Tap to try again?";
      return Padding(
        padding: const EdgeInsets.fromLTRB(40, 20, 40, 0),
        child: Text(
          msg,
          textAlign: TextAlign.center,
          style: TextStyle(
            color: AppColors.error.withValues(alpha: 0.85),
            fontSize: 13,
            height: 1.5,
          ),
        ),
      );
    }

    if (hasTranscript) {
      WidgetsBinding.instance.addPostFrameCallback((_) {
        if (!_scrollController.hasClients) return;
        _scrollController.animateTo(
          _scrollController.position.maxScrollExtent,
          duration: const Duration(milliseconds: 220),
          curve: Curves.easeOutCubic,
        );
      });
    }
    return AnimatedSize(
      duration: const Duration(milliseconds: 250),
      curve: Curves.easeOut,
      child: hasTranscript
          ? Padding(
              padding: const EdgeInsets.fromLTRB(30, 8, 30, 0),
              child: ShaderMask(
                shaderCallback: (bounds) {
                  return LinearGradient(
                    begin: Alignment.topCenter,
                    end: Alignment.bottomCenter,
                    colors: [
                      Colors.transparent,
                      Colors.white,
                      Colors.white,
                      Colors.transparent,
                    ],
                    stops: const [0, 0.12, 0.86, 1],
                  ).createShader(bounds);
                },
                blendMode: BlendMode.dstIn,
                child: ConstrainedBox(
                  constraints: BoxConstraints(
                    maxHeight: MediaQuery.of(context).size.height * 0.55,
                  ),
                  child: ListView.separated(
                    controller: _scrollController,
                    shrinkWrap: true,
                    padding: const EdgeInsets.symmetric(vertical: 22),
                    itemCount: entries.length,
                    separatorBuilder: (_, _) => const SizedBox(height: 14),
                    itemBuilder: (_, index) {
                      return _VoiceTranscriptLine(entry: entries[index]);
                    },
                  ),
                ),
              ),
            )
          : const SizedBox.shrink(),
    );
  }
}

class _VoiceTranscriptLine extends StatelessWidget {
  final VoiceTranscriptEntry entry;

  const _VoiceTranscriptLine({required this.entry});

  @override
  Widget build(BuildContext context) {
    final isUser = entry.role == VoiceTranscriptRole.user;
    final isTool = entry.role == VoiceTranscriptRole.tool;
    final color = isUser
        ? AppColors.textPrimary
        : isTool
            ? AppColors.accentDark.withValues(alpha: 0.80)
            : AppColors.textSecondary;

    return AnimatedOpacity(
      duration: const Duration(milliseconds: 180),
      opacity: entry.isFinal ? 1 : 0.64,
      child: Text(
        entry.text,
        textAlign: TextAlign.center,
        style: TextStyle(
          color: color,
          fontSize: isTool ? 13 : 16,
          height: 1.5,
          fontWeight: isUser ? FontWeight.w600 : FontWeight.w500,
          fontStyle: isTool ? FontStyle.italic : FontStyle.normal,
        ),
      ),
    );
  }
}

// Voice chat ended — rating card + feedback dialog

const List<String> _voiceLikeReasons = [
  'Fast',
  'Understood me',
  'Felt natural',
  'Helpful',
];

const List<String> _voiceDislikeReasons = [
  'Too slow',
  "Didn't listen",
  'Not engaging',
  'Misunderstood me',
];

String _formatVoiceDuration(Duration d) {
  final minutes = d.inMinutes;
  final seconds = d.inSeconds % 60;
  if (minutes > 0) return '${minutes}m ${seconds}s';
  return '${seconds}s';
}

Future<void> _showVoiceFeedbackDialog(
  BuildContext context,
  HomeViewModel vm, {
  required bool liked,
}) async {
  final result = await showDialog<({List<String> reasons, String note})>(
    context: context,
    barrierDismissible: false,
    builder: (_) => _VoiceFeedbackDialog(liked: liked),
  );
  // Dialog is non-dismissible, but guard anyway.
  final reasons = result?.reasons ?? const <String>[];
  final note = result?.note ?? '';
  final error = await vm.submitVoiceSessionRating(
    liked: liked,
    reasons: reasons,
    note: note,
  );
  vm.dismissEndedSummary();
  if (!context.mounted) return;
  ScaffoldMessenger.of(context).showSnackBar(
    SnackBar(
      content: Text(
        error ?? 'Got it — thanks for the feedback.',
        style: const TextStyle(color: AppColors.textPrimary),
      ),
      backgroundColor:
          error == null ? AppColors.surfaceVariant : AppColors.errorSurface,
      behavior: SnackBarBehavior.floating,
    ),
  );
}

class _VoiceEndedCard extends StatefulWidget {
  final VoiceSessionEndedSummary summary;
  final VoidCallback onLike;
  final VoidCallback onDislike;
  final VoidCallback onAutoDismiss;

  const _VoiceEndedCard({
    super.key,
    required this.summary,
    required this.onLike,
    required this.onDislike,
    required this.onAutoDismiss,
  });

  @override
  State<_VoiceEndedCard> createState() => _VoiceEndedCardState();
}

class _VoiceEndedCardState extends State<_VoiceEndedCard> {
  Timer? _dismissTimer;

  @override
  void initState() {
    super.initState();
    _dismissTimer = Timer(const Duration(seconds: 10), widget.onAutoDismiss);
  }

  @override
  void dispose() {
    _dismissTimer?.cancel();
    super.dispose();
  }

  // A rating opens a dialog — stop the auto-dismiss so the card doesn't vanish
  // out from under it.
  void _cancelAutoDismiss() => _dismissTimer?.cancel();

  @override
  Widget build(BuildContext context) {
    // Solid surface instead of glass card, to ensure readability of the feedback options
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 8),
      decoration: BoxDecoration(
        color: AppColors.surfaceVariant,
        borderRadius: BorderRadius.circular(16),
        border: Border.all(color: AppColors.glassBorderLight, width: 1),
      ),
      child: Row(
        children: [
          Expanded(
            child: Text(
              'Voice chat ended · ${_formatVoiceDuration(widget.summary.duration)}',
              style: const TextStyle(
                color: AppColors.textSecondary,
                fontSize: 14,
                fontWeight: FontWeight.w500,
              ),
            ),
          ),
          IconButton(
            icon: const Icon(Icons.thumb_up_alt_outlined),
            color: AppColors.textSecondary,
            iconSize: 20,
            tooltip: 'Liked it',
            onPressed: () {
              _cancelAutoDismiss();
              widget.onLike();
            },
          ),
          IconButton(
            icon: const Icon(Icons.thumb_down_alt_outlined),
            color: AppColors.textSecondary,
            iconSize: 20,
            tooltip: "Didn't like it",
            onPressed: () {
              _cancelAutoDismiss();
              widget.onDislike();
            },
          ),
        ],
      ),
    );
  }
}

class _VoiceFeedbackDialog extends StatefulWidget {
  final bool liked;

  const _VoiceFeedbackDialog({required this.liked});

  @override
  State<_VoiceFeedbackDialog> createState() => _VoiceFeedbackDialogState();
}

class _VoiceFeedbackDialogState extends State<_VoiceFeedbackDialog> {
  final Set<String> _selected = {};
  final TextEditingController _noteController = TextEditingController();

  @override
  void dispose() {
    _noteController.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final options = widget.liked ? _voiceLikeReasons : _voiceDislikeReasons;
    return AlertDialog(
      backgroundColor: AppColors.surface,
      title: Text(
        widget.liked ? 'What worked?' : 'What went wrong?',
        style: const TextStyle(color: AppColors.textPrimary, fontSize: 18),
      ),
      content: SingleChildScrollView(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Wrap(
              spacing: 8,
              runSpacing: 8,
              children: [
                for (final option in options)
                  _FeedbackPill(
                    label: option,
                    selected: _selected.contains(option),
                    onTap: () => setState(() {
                      if (!_selected.add(option)) _selected.remove(option);
                    }),
                  ),
              ],
            ),
            const SizedBox(height: 16),
            TextField(
              controller: _noteController,
              maxLines: 3,
              minLines: 1,
              style: const TextStyle(color: AppColors.textPrimary),
              decoration: InputDecoration(
                hintText: 'Anything else? (optional)',
                hintStyle: const TextStyle(color: AppColors.textTertiary),
                filled: true,
                fillColor: AppColors.surfaceVariant,
                border: OutlineInputBorder(
                  borderRadius: BorderRadius.circular(12),
                  borderSide: BorderSide.none,
                ),
                enabledBorder: OutlineInputBorder(
                  borderRadius: BorderRadius.circular(12),
                  borderSide: BorderSide.none,
                ),
                focusedBorder: OutlineInputBorder(
                  borderRadius: BorderRadius.circular(12),
                  borderSide: BorderSide.none,
                ),
              ),
            ),
          ],
        ),
      ),
      actions: [
        TextButton(
          onPressed: () => Navigator.of(context).pop(
            (reasons: _selected.toList(), note: _noteController.text.trim()),
          ),
          child: const Text('Submit', style: TextStyle(color: AppColors.accent)),
        ),
      ],
    );
  }
}

class _FeedbackPill extends StatelessWidget {
  final String label;
  final bool selected;
  final VoidCallback onTap;

  const _FeedbackPill({
    required this.label,
    required this.selected,
    required this.onTap,
  });

  @override
  Widget build(BuildContext context) {
    return GestureDetector(
      onTap: onTap,
      child: AnimatedContainer(
        duration: const Duration(milliseconds: 150),
        padding: const EdgeInsets.symmetric(horizontal: 11, vertical: 7),
        decoration: BoxDecoration(
          color: selected
              ? AppColors.accent.withValues(alpha: 0.18)
              : AppColors.glassWhiteFill,
          borderRadius: BorderRadius.circular(18),
          border: Border.all(
            color: selected ? AppColors.accent : AppColors.glassBorderLight,
            width: 1,
          ),
        ),
        child: Text(
          label,
          style: TextStyle(
            color: selected ? AppColors.accent : AppColors.textSecondary,
            fontSize: 12,
            fontWeight: selected ? FontWeight.w600 : FontWeight.w500,
          ),
        ),
      ),
    );
  }
}

// Drawer

class _ChatDrawer extends StatelessWidget {
  final VoidCallback onNewChat;
  final void Function(String sessionId) onSelectSession;

  const _ChatDrawer({required this.onNewChat, required this.onSelectSession});

  void _openSettings(BuildContext context) {
    Navigator.of(context).pop();
    Navigator.push(
      context,
      MaterialPageRoute<void>(builder: (_) => const SettingsScreen()),
    );
  }

  @override
  Widget build(BuildContext context) {
    final width = MediaQuery.of(context).size.width;
    return Drawer(
      backgroundColor: AppColors.background,
      width: width * 0.82,
      child: SafeArea(
        child: Consumer<AuthViewModel>(
          builder: (_, authVm, _) {
            final isLoggedIn = authVm.user != null;

            return Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                // Wordmark
                const Padding(
                  padding: EdgeInsets.fromLTRB(24, 18, 24, 16),
                  child: Text(
                    'Aura',
                    style: TextStyle(
                      fontFamily: 'Outfit',
                      color: AppColors.textPrimary,
                      fontSize: 26,
                      fontWeight: FontWeight.w700,
                      letterSpacing: -0.5,
                    ),
                  ),
                ),

                if (isLoggedIn) ...[
                  _DrawerNavRow(
                    icon: Icons.edit_outlined,
                    label: 'New chat',
                    onTap: onNewChat,
                  ),
                  _DrawerNavRow(
                    icon: Icons.auto_awesome_outlined,
                    label: 'Your Aura Profile',
                    onTap: () {
                      Navigator.of(context).pop();
                      Navigator.push(context, AuraProfileScreen.route());
                    },
                  ),
                  _DrawerNavRow(
                    icon: Icons.article_outlined,
                    label: 'Daily briefing',
                    onTap: () {
                      Navigator.of(context).pop();
                      context.push('/briefing');
                    },
                  ),
                  _DrawerNavRow(
                    icon: Icons.trending_up_rounded,
                    label: 'Get Better',
                    onTap: () {
                      Navigator.of(context).pop();
                      context.push('/get-better');
                    },
                  ),
                  const SizedBox(height: 6),
                  Expanded(
                    child: _SessionList(onSelectSession: onSelectSession),
                  ),
                  const _DrawerDivider(),
                  _DrawerNavRow(
                    icon: Icons.help_outline_rounded,
                    label: 'Help & feedback',
                    onTap: () {
                      Navigator.of(context).pop();
                      showFeedbackSheet(context);
                    },
                  ),
                  _DrawerNavRow(
                    icon: Icons.settings_outlined,
                    label: 'Settings',
                    onTap: () => _openSettings(context),
                  ),
                  const SizedBox(height: 8),
                ] else ...[
                  // Signed-out: sign-in prompt, then Settings pinned at bottom.
                  Padding(
                    padding: const EdgeInsets.fromLTRB(20, 8, 20, 8),
                    child: GestureDetector(
                      onTap: () {
                        Navigator.of(context).pop();
                        context.go('/login');
                      },
                      child: FauxGlassCard(
                        borderRadius: 14,
                        padding: const EdgeInsets.symmetric(vertical: 14),
                        borderColor: AppColors.accent.withValues(alpha: 0.4),
                        gradient: LinearGradient(
                          begin: Alignment.topLeft,
                          end: Alignment.bottomRight,
                          colors: [
                            AppColors.accent.withValues(alpha: 0.16),
                            AppColors.accent.withValues(alpha: 0.07),
                          ],
                        ),
                        child: Row(
                          mainAxisAlignment: MainAxisAlignment.center,
                          children: [
                            Icon(Icons.login_rounded,
                                color: AppColors.accentDark, size: 18),
                            const SizedBox(width: 8),
                            Text(
                              'Sign In',
                              style: TextStyle(
                                color: AppColors.accentDark,
                                fontSize: 15,
                                fontWeight: FontWeight.w600,
                              ),
                            ),
                          ],
                        ),
                      ),
                    ),
                  ),
                  const Expanded(
                    child: Center(
                      child: Column(
                        mainAxisSize: MainAxisSize.min,
                        children: [
                          Icon(Icons.history_rounded,
                              color: AppColors.textTertiary, size: 40),
                          SizedBox(height: 12),
                          Text(
                            'Sign in to see your chat history',
                            style: TextStyle(
                              color: AppColors.textTertiary,
                              fontSize: 13,
                            ),
                          ),
                        ],
                      ),
                    ),
                  ),
                  const _DrawerDivider(),
                  _DrawerNavRow(
                    icon: Icons.settings_outlined,
                    label: 'Settings',
                    onTap: () => _openSettings(context),
                  ),
                  const SizedBox(height: 8),
                ],
              ],
            );
          },
        ),
      ),
    );
  }
}

// A single Pi-style drawer row: outline icon + label, no card.
class _DrawerNavRow extends StatelessWidget {
  final IconData icon;
  final String label;
  final VoidCallback onTap;

  const _DrawerNavRow({
    required this.icon,
    required this.label,
    required this.onTap,
  });

  @override
  Widget build(BuildContext context) {
    return Material(
      color: Colors.transparent,
      child: InkWell(
        onTap: onTap,
        child: Padding(
          padding: const EdgeInsets.symmetric(horizontal: 24, vertical: 13),
          child: Row(
            children: [
              Icon(icon, color: AppColors.textSecondary, size: 22),
              const SizedBox(width: 16),
              Text(
                label,
                style: const TextStyle(
                  color: AppColors.textPrimary,
                  fontSize: 16,
                  fontWeight: FontWeight.w500,
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}

class _DrawerDivider extends StatelessWidget {
  const _DrawerDivider();

  @override
  Widget build(BuildContext context) {
    return const Padding(
      padding: EdgeInsets.symmetric(horizontal: 16),
      child: Divider(color: AppColors.divider, height: 1),
    );
  }
}

class _SolidScrollBehavior extends ScrollBehavior {
  const _SolidScrollBehavior();

  @override
  Widget buildOverscrollIndicator(
    BuildContext context,
    Widget child,
    ScrollableDetails details,
  ) =>
      child;
}

class _SessionList extends StatefulWidget {
  final void Function(String sessionId) onSelectSession;
  const _SessionList({required this.onSelectSession});

  @override
  State<_SessionList> createState() => _SessionListState();
}

class _SessionListState extends State<_SessionList> {
  List<ChatSession> _sessions = [];
  bool _loaded = false;
  bool _hasMore = true;
  bool _loadingMore = false;
  final _scrollController = ScrollController();

  static const _pageSize = 25;

  @override
  void initState() {
    super.initState();
    _load();
    _scrollController.addListener(_onScroll);
  }

  @override
  void dispose() {
    _scrollController.dispose();
    super.dispose();
  }

  void _onScroll() {
    if (_loadingMore || !_hasMore) return;
    if (_scrollController.position.pixels >=
        _scrollController.position.maxScrollExtent - 80) {
      _loadMore();
    }
  }

  Future<void> _load() async {
    final repo = context.read<ChatRepository>();
    final uid = context.read<AuthViewModel>().user?.uid ?? '';
    // Hydrate the local store from the Firestore backup before reading it. The
    // drawer is the chat-history surface and may be opened on a fresh install
    // (or after a cache clear) before any chat screen has run a restore — so the
    // read path owns its own hydration instead of depending on a sibling widget
    // having been built. restoreFromBackupIfLocalEmpty is idempotent and a cheap
    // no-op (single count query) once local sessions exist.
    if (uid.isNotEmpty && mounted) {
      await context.read<ChatBackupService>().restoreFromBackupIfLocalEmpty(uid);
    }
    if (!mounted) return;
    final result = await repo.loadMainSessions(userId: uid, limit: _pageSize);
    result.when(
      success: (s) => setState(() {
        _sessions = s;
        _loaded = true;
        _hasMore = s.length == _pageSize;
      }),
      failure: (_) => setState(() => _loaded = true),
    );
  }

  Future<void> _loadMore() async {
    if (_loadingMore || !_hasMore) return;
    setState(() => _loadingMore = true);
    final repo = context.read<ChatRepository>();
    final uid = context.read<AuthViewModel>().user?.uid ?? '';
    final result = await repo.loadMainSessions(
      userId: uid,
      limit: _pageSize,
      offset: _sessions.length,
    );
    result.when(
      success: (s) => setState(() {
        _sessions.addAll(s);
        _hasMore = s.length == _pageSize;
        _loadingMore = false;
      }),
      failure: (_) => setState(() => _loadingMore = false),
    );
  }

  @override
  Widget build(BuildContext context) {
    if (!_loaded) {
      return const Center(
        child: SizedBox(
          width: 20,
          height: 20,
          child: CircularProgressIndicator(strokeWidth: 2),
        ),
      );
    }
    if (_sessions.isEmpty) {
      return const Padding(
        padding: EdgeInsets.fromLTRB(24, 12, 24, 0),
        child: Text('No recent chats',
            style: TextStyle(color: AppColors.textTertiary, fontSize: 13)),
      );
    }

    // Flatten sessions into a lightweight row model (header markers + session
    // refs). The model is cheap data work; ListView.builder then inflates only
    // the visible header/tile WIDGETS on demand, so scrolling and pagination
    // never reconstruct the whole list. The list is already newest-first.
    final rows = _buildRowModel();

    return ScrollConfiguration(
      behavior: const _SolidScrollBehavior(),
      child: ListView.builder(
        controller: _scrollController,
        padding: const EdgeInsets.only(top: 2),
        itemCount: rows.length + (_loadingMore ? 1 : 0),
        itemBuilder: (_, i) {
          if (i == rows.length) {
            return const Padding(
              padding: EdgeInsets.symmetric(vertical: 12),
              child: Center(
                child: SizedBox(
                  width: 16,
                  height: 16,
                  child: CircularProgressIndicator(strokeWidth: 1.5),
                ),
              ),
            );
          }
          final row = rows[i];
          if (row.header) return _DrawerSectionHeader(row.text);
          return _DrawerSessionTile(
            key: ValueKey(row.id),
            label: row.text,
            onTap: () => widget.onSelectSession(row.id!),
          );
        },
      ),
    );
  }

  /// Cheap data model for the drawer list: a date-group header marker followed
  /// by its session rows. Headers carry no id; sessions carry their id + title.
  List<({bool header, String text, String? id})> _buildRowModel() {
    final rows = <({bool header, String text, String? id})>[];
    String? currentGroup;
    for (final s in _sessions) {
      final group = _groupLabel(s.startedAt);
      if (group != currentGroup) {
        currentGroup = group;
        rows.add((header: true, text: group, id: null));
      }
      rows.add((
        header: false,
        text: s.title?.isNotEmpty == true ? s.title! : 'New chat',
        id: s.id,
      ));
    }
    return rows;
  }

  String _groupLabel(DateTime dt) {
    final now = DateTime.now();
    final today = DateTime(now.year, now.month, now.day);
    final day = DateTime(dt.year, dt.month, dt.day);
    final diff = today.difference(day).inDays;
    if (diff <= 0) return 'Today';
    if (diff == 1) return 'Yesterday';
    if (diff < 7) return 'Previous 7 days';
    if (diff < 30) return 'Previous 30 days';
    return 'Older';
  }
}

class _DrawerSectionHeader extends StatelessWidget {
  final String label;
  const _DrawerSectionHeader(this.label);

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.fromLTRB(24, 16, 24, 6),
      child: Text(
        label,
        style: const TextStyle(
          color: AppColors.textTertiary,
          fontSize: 13,
          fontWeight: FontWeight.w600,
        ),
      ),
    );
  }
}

class _DrawerSessionTile extends StatelessWidget {
  final String label;
  final VoidCallback onTap;

  const _DrawerSessionTile({super.key, required this.label, required this.onTap});

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 1),
      child: Material(
        color: Colors.transparent,
        child: InkWell(
          onTap: onTap,
          borderRadius: BorderRadius.circular(12),
          child: Padding(
            padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 12),
            child: Text(
              label,
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
              style: const TextStyle(
                color: AppColors.textPrimary,
                fontSize: 15,
              ),
            ),
          ),
        ),
      ),
    );
  }
}
