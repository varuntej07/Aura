import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';
import 'package:provider/provider.dart';

import '../../../core/theme/app_colors.dart';
import '../../../core/theme/glass_card.dart';
import '../../../data/local/app_database.dart';
import '../../../data/models/voice_models.dart';
import '../../../data/repositories/chat_repository.dart';
import '../../../data/services/chat_service_provider.dart';
import '../../../data/services/chat_backup_service.dart';
import '../../../data/services/chat_session_manager.dart';
import '../../../data/services/feedback_service.dart';
import '../../../data/services/posthog_analytics_service.dart';
import '../../../core/network/connectivity_service.dart';
import '../../viewmodels/auth_viewmodel.dart';
import '../../viewmodels/home_viewmodel.dart';
import '../../viewmodels/text_chat_viewmodel.dart';
import '../chat/embedded_chat_panel.dart';
import '../settings/settings_screen.dart';

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
          extra: {
            'engagementId': payload.engagementId,
            'agentContext': payload.agentContext,
            'initialMessage': payload.initialMessage,
          },
        );
      };

      vm.onAgentNudgeTap = (payload) {
        context.push(
          '/agents/${payload.agentId}',
          extra: payload.chatOpener.isNotEmpty ? payload.chatOpener : null,
        );
      };

      vm.onSignalNotificationTap = (payload) {
        context.push(
          '/chat/new',
          extra: {
            'initialMessage': payload.openingChatMessage,
            'signalNotificationId': payload.notificationId,
            'signalContentId': payload.contentId,
            'signalCategory': payload.category,
          },
        );
      };

      if (uid != null && uid.isNotEmpty) {
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
    setState(() => _mode = mode);
    _pageController.animateToPage(
      mode.index,
      duration: const Duration(milliseconds: 360),
      curve: Curves.easeOutCubic,
    );
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      key: _scaffoldKey,
      backgroundColor: Colors.transparent,
      resizeToAvoidBottomInset: false,
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
                  GlassIconButton(
                    icon: Icons.settings_outlined,
                    onTap: () => Navigator.push(
                      context,
                      MaterialPageRoute<void>(
                          builder: (_) => const SettingsScreen()),
                    ),
                  ),
                ],
              ),
            ),
            Expanded(
              child: PageView(
                controller: _pageController,
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
                color: selected ? AppColors.accentLight : AppColors.textTertiary,
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
    final bottomReserve = MediaQuery.of(context).viewPadding.bottom + 110;

    return Consumer<HomeViewModel>(
      builder: (_, vm, _) {
        return Stack(
          children: [
            Positioned.fill(
              bottom: bottomReserve + 132,
              child: Align(
                alignment: Alignment.topCenter,
                child: SingleChildScrollView(
                  padding: const EdgeInsets.fromLTRB(20, 2, 20, 12),
                  child: _VoiceStatusCard(vm: vm),
                ),
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

  Color _colorOf(MicState state) => switch (state) {
        MicState.idle => AppColors.micIdle,
        MicState.listening => AppColors.micListening,
        MicState.processing => AppColors.micProcessing,
      };

  @override
  Widget build(BuildContext context) {
    final label = _labelOf(widget.voiceStatus);
    final color = _colorOf(widget.micState);
    return GestureDetector(
      onTap: widget.onTap,
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          AnimatedBuilder(
            animation: Listenable.merge([widget.breathAnimation, widget.rippleAnimation]),
            builder: (_, _) {
              final isActive = widget.micState != MicState.idle;
              final scale = isActive ? 1.0 : widget.breathAnimation.value;
              return Transform.scale(
                scale: scale,
                child: Stack(
                  alignment: Alignment.center,
                  children: [
                    // Ripple ring — visible while listening
                    if (widget.micState == MicState.listening)
                      Transform.scale(
                        scale: widget.rippleAnimation.value,
                        child: Container(
                          width: 100,
                          height: 100,
                          decoration: BoxDecoration(
                            shape: BoxShape.circle,
                            color: color.withValues(
                              alpha: (1 - widget.rippleAnimation.value + 1)
                                  .clamp(0, 0.25),
                            ),
                          ),
                        ),
                      ),
                    // Main button
                    Container(
                      width: 80,
                      height: 80,
                      decoration: BoxDecoration(
                        shape: BoxShape.circle,
                        color: color,
                        boxShadow: [
                          BoxShadow(
                            color: color.withValues(alpha: 0.45),
                            blurRadius: 24,
                            spreadRadius: 4,
                          ),
                        ],
                      ),
                      child: const Icon(
                        Icons.mic_rounded,
                        color: Colors.white,
                        size: 36,
                      ),
                    ),
                  ],
                ),
              );
            },
          ),
          const SizedBox(height: 20),
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
                    maxHeight: MediaQuery.of(context).size.height * 0.46,
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
        ? Colors.white.withValues(alpha: 0.96)
        : isTool
            ? AppColors.accentLight.withValues(alpha: 0.72)
            : Colors.white.withValues(alpha: 0.78);

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

// Drawer

class _ChatDrawer extends StatelessWidget {
  final VoidCallback onNewChat;
  final void Function(String sessionId) onSelectSession;

  const _ChatDrawer({required this.onNewChat, required this.onSelectSession});

  @override
  Widget build(BuildContext context) {
    return Drawer(
      backgroundColor: AppColors.deepBackground,
      child: SafeArea(
        child: Consumer<AuthViewModel>(
          builder: (_, authVm, _) {
            final isLoggedIn = authVm.user != null;

            return Column(
              children: [
                // Profile / sign-in header
                if (isLoggedIn)
                  Padding(
                    padding: const EdgeInsets.fromLTRB(20, 24, 20, 16),
                    child: Row(
                      children: [
                        Container(
                          width: 40,
                          height: 40,
                          decoration: BoxDecoration(
                            color: AppColors.accent.withValues(alpha: 0.15),
                            shape: BoxShape.circle,
                            border: Border.all(
                                color: AppColors.glassBorderDim, width: 1),
                          ),
                          child: const Icon(Icons.person_rounded,
                              color: AppColors.accent, size: 22),
                        ),
                        const SizedBox(width: 12),
                        Expanded(
                          child: Column(
                            crossAxisAlignment: CrossAxisAlignment.start,
                            children: [
                              Text(
                                authVm.user?.displayName ?? 'User',
                                style: const TextStyle(
                                  color: AppColors.textPrimary,
                                  fontSize: 15,
                                  fontWeight: FontWeight.w600,
                                ),
                                maxLines: 1,
                                overflow: TextOverflow.ellipsis,
                              ),
                              if ((authVm.user?.email ?? '').isNotEmpty)
                                Text(
                                  authVm.user?.email ?? '',
                                  style: const TextStyle(
                                      color: AppColors.textTertiary,
                                      fontSize: 12),
                                  maxLines: 1,
                                  overflow: TextOverflow.ellipsis,
                                ),
                            ],
                          ),
                        ),
                      ],
                    ),
                  )
                else
                  Padding(
                    padding: const EdgeInsets.fromLTRB(20, 32, 20, 24),
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
                            AppColors.accent.withValues(alpha: 0.18),
                            AppColors.accent.withValues(alpha: 0.08),
                          ],
                        ),
                        child: const Row(
                          mainAxisAlignment: MainAxisAlignment.center,
                          children: [
                            Icon(Icons.login_rounded,
                                color: AppColors.accent, size: 18),
                            SizedBox(width: 8),
                            Text(
                              'Sign In',
                              style: TextStyle(
                                color: AppColors.accent,
                                fontSize: 15,
                                fontWeight: FontWeight.w600,
                              ),
                            ),
                          ],
                        ),
                      ),
                    ),
                  ),

                const Divider(color: AppColors.divider, height: 1),

                if (isLoggedIn) ...[
                  ListTile(
                    leading: const Icon(Icons.add_rounded,
                        color: AppColors.accent),
                    title: const Text('New Chat',
                        style: TextStyle(
                            color: AppColors.textPrimary, fontSize: 14)),
                    onTap: onNewChat,
                  ),
                  const Divider(color: AppColors.divider, height: 1),
                  Expanded(
                    child: _SessionList(onSelectSession: onSelectSession),
                  ),
                ] else
                  Expanded(
                    child: Center(
                      child: Column(
                        mainAxisSize: MainAxisSize.min,
                        children: [
                          const Icon(Icons.history_rounded,
                              color: AppColors.textTertiary, size: 40),
                          const SizedBox(height: 12),
                          const Text(
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
              ],
            );
          },
        ),
      ),
    );
  }
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
        padding: EdgeInsets.all(20),
        child: Text('No recent chats',
            style: TextStyle(color: AppColors.textTertiary, fontSize: 13)),
      );
    }
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        const Padding(
          padding: EdgeInsets.fromLTRB(16, 12, 16, 4),
          child: Text(
            'RECENT CHATS',
            style: TextStyle(
              color: AppColors.textTertiary,
              fontSize: 11,
              fontWeight: FontWeight.w600,
              letterSpacing: 0.5,
            ),
          ),
        ),
        Expanded(
          child: ListView.builder(
            controller: _scrollController,
            itemCount: _sessions.length + (_loadingMore ? 1 : 0),
            itemBuilder: (_, i) {
              if (i == _sessions.length) {
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
              final s = _sessions[i];
              final label = s.title?.isNotEmpty == true ? s.title! : 'Chat ${i + 1}';
              return ListTile(
                title: Text(
                  label,
                  style: const TextStyle(
                      color: AppColors.textPrimary, fontSize: 13),
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                ),
                subtitle: Text(
                  _formatDate(s.startedAt),
                  style: const TextStyle(
                      color: AppColors.textTertiary, fontSize: 11),
                ),
                onTap: () => widget.onSelectSession(s.id),
              );
            },
          ),
        ),
      ],
    );
  }

  String _formatDate(DateTime dt) {
    final diff = DateTime.now().difference(dt);
    if (diff.inDays == 0) return 'Today';
    if (diff.inDays == 1) return 'Yesterday';
    if (diff.inDays < 7) return '${diff.inDays} days ago';
    return '${dt.month}/${dt.day}/${dt.year}';
  }
}
