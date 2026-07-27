import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:go_router/go_router.dart';
import 'package:provider/provider.dart';

import '../../../core/theme/app_colors.dart';
import '../../../core/theme/glass_card.dart';
import '../../../data/models/get_better_feed.dart';
import '../../../data/repositories/get_better_repository.dart';
import '../../viewmodels/auth_viewmodel.dart';
import '../../viewmodels/get_better_viewmodel.dart';
import '../../viewmodels/text_chat_viewmodel.dart';
import '../../widgets/chat_message_list.dart';
import '../../widgets/error_display.dart';
import '../../widgets/message_input.dart';
import '../reminders/reminders_screen.dart';

class GetBetterScreen extends StatefulWidget {
  const GetBetterScreen({super.key});

  @override
  State<GetBetterScreen> createState() => _GetBetterScreenState();
}

class _GetBetterScreenState extends State<GetBetterScreen> {
  final _chatScrollController = ScrollController();
  final _inputController = TextEditingController();
  final _inputFocusNode = FocusNode();

  bool _showChat = false;
  bool _chatReady = false;
  GetBetterIdea? _focusedIdea;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addPostFrameCallback((_) async {
      if (!mounted) return;
      final userId = context.read<AuthViewModel>().user?.uid;
      if (userId == null || userId.isEmpty) return;
      context.read<GetBetterViewModel>().load(userId);
      await context.read<TextChatViewModel>().init(userId);
      if (mounted) setState(() => _chatReady = true);
    });
  }

  @override
  void dispose() {
    _chatScrollController.dispose();
    _inputController.dispose();
    _inputFocusNode.dispose();
    super.dispose();
  }

  void _scrollChatToBottom() {
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (_chatScrollController.hasClients) {
        _chatScrollController.animateTo(
          _chatScrollController.position.maxScrollExtent,
          duration: const Duration(milliseconds: 260),
          curve: Curves.easeOutCubic,
        );
      }
    });
  }

  void _sendMessage(
    GetBetterViewModel getBetterViewModel,
    TextChatViewModel chatViewModel,
    String text,
  ) {
    final userId = context.read<AuthViewModel>().user?.uid;
    if (!_chatReady || userId == null || text.trim().isEmpty) return;

    final chatStarted =
        chatViewModel.messages.isNotEmpty || chatViewModel.isStreaming;
    setState(() => _showChat = true);
    if (chatStarted) {
      chatViewModel.sendMessage(text, userId);
    } else {
      chatViewModel.startGetBetterConversation(
        contextMessage: getBetterViewModel.conversationContext(
          focusedIdea: _focusedIdea,
        ),
        firstUserMessage: text,
      );
    }
    _scrollChatToBottom();
  }

  void _bringIdeaIntoChat(GetBetterIdea idea) {
    setState(() {
      _focusedIdea = idea;
      _showChat = true;
    });
    _inputController
      ..text = idea.chatPrompt
      ..selection = TextSelection.collapsed(offset: idea.chatPrompt.length);
    _inputFocusNode.requestFocus();
  }

  Future<void> _openIdea(
    GetBetterIdea idea,
    Map<String, File> cachedImages,
    GetBetterViewModel viewModel, {
    bool fromRelatedStory = false,
  }) async {
    await viewModel.recordOpened(idea, fromRelatedStory: fromRelatedStory);
    if (!mounted) return;
    await showModalBottomSheet<void>(
      context: context,
      isScrollControlled: true,
      useSafeArea: true,
      backgroundColor: Colors.transparent,
      barrierColor: AppColors.textPrimary.withValues(alpha: 0.28),
      builder: (sheetContext) => ChangeNotifierProvider.value(
        value: viewModel,
        child: Consumer<GetBetterViewModel>(
          builder: (context, currentViewModel, _) => _IdeaDetailSheet(
            idea: idea,
            imageFile: cachedImages[idea.imageKey],
            relatedStories: currentViewModel.relatedStories(idea),
            cachedImages: cachedImages,
            state: currentViewModel.storyState(idea.id),
            onToggleSaved: () => currentViewModel.toggleSaved(idea),
            onToggleCompleted: () => currentViewModel.toggleCompleted(idea),
            onShare: () => _shareStory(idea, currentViewModel),
            onOpenRelated: (related) {
              Navigator.of(sheetContext).pop();
              WidgetsBinding.instance.addPostFrameCallback((_) {
                if (!mounted) return;
                _openIdea(
                  related,
                  currentViewModel.cachedImages,
                  currentViewModel,
                  fromRelatedStory: true,
                );
              });
            },
            onTalk: () {
              currentViewModel.recordBuddyChatStarted(idea);
              Navigator.of(sheetContext).pop();
              _bringIdeaIntoChat(idea);
            },
          ),
        ),
      ),
    );
  }

  Future<void> _shareStory(
    GetBetterIdea idea,
    GetBetterViewModel viewModel,
  ) async {
    final shareText =
        '${idea.title}\n\n${idea.narrative}\n\n'
        'What it means: ${idea.whatItMeans}\n\n'
        'Try this: ${idea.tryThis}\n\n'
        'From Aura Get Better';
    await Clipboard.setData(ClipboardData(text: shareText));
    await viewModel.recordShared(idea);
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(
      const SnackBar(content: Text('Story copied. It is ready to share.')),
    );
  }

  @override
  Widget build(BuildContext context) {
    return AmbientBackground(
      child: Scaffold(
        backgroundColor: Colors.transparent,
        resizeToAvoidBottomInset: true,
        body: SafeArea(
          child: Consumer2<GetBetterViewModel, TextChatViewModel>(
            builder: (context, getBetterViewModel, chatViewModel, _) {
              if (chatViewModel.chatLimitReached) {
                WidgetsBinding.instance.addPostFrameCallback((_) {
                  chatViewModel.clearChatLimitReached();
                  context.push('/paywall');
                });
              }

              return Column(
                children: [
                  _SheetHeader(
                    showingChat: _showChat,
                    onClose: () => context.pop(),
                    onShowIdeas: () {
                      FocusScope.of(context).unfocus();
                      setState(() => _showChat = false);
                    },
                  ),
                  if (getBetterViewModel.loading)
                    const LinearProgressIndicator(
                      minHeight: 2,
                      color: AppColors.accent,
                      backgroundColor: Colors.transparent,
                    ),
                  Expanded(
                    child: AnimatedSwitcher(
                      duration: const Duration(milliseconds: 260),
                      switchInCurve: Curves.easeOutCubic,
                      switchOutCurve: Curves.easeInCubic,
                      child: _showChat
                          ? ChatMessageList(
                              key: const ValueKey('get-better-chat'),
                              messages: chatViewModel.messages,
                              scrollController: _chatScrollController,
                              isStreaming: chatViewModel.isStreaming,
                              streamingOutput: chatViewModel.streamingOutput,
                              onRetry: chatViewModel.retryLastMessage,
                              onEdit: chatViewModel.editAndResend,
                              onFeedback: chatViewModel.setFeedback,
                              onViewReminders: () => Navigator.push(
                                context,
                                RemindersScreen.route(),
                              ),
                              onClarificationSubmit:
                                  chatViewModel.submitClarification,
                            )
                          : _DiscoveryBody(
                              key: const ValueKey('get-better-discovery'),
                              viewModel: getBetterViewModel,
                              onOpenIdea: (idea) => _openIdea(
                                idea,
                                getBetterViewModel.cachedImages,
                                getBetterViewModel,
                              ),
                            ),
                    ),
                  ),
                  if (chatViewModel.error != null)
                    Padding(
                      padding: const EdgeInsets.fromLTRB(16, 4, 16, 0),
                      child: ErrorDisplay(
                        error: chatViewModel.error!,
                        onDismiss: chatViewModel.clearError,
                      ),
                    ),
                  MessageInput(
                    controller: _inputController,
                    focusNode: _inputFocusNode,
                    isLoading:
                        !_chatReady || chatViewModel.state == ViewState.loading,
                    hint: !_chatReady
                        ? 'Getting Buddy ready'
                        : _focusedIdea == null
                        ? 'Talk this through with Buddy'
                        : 'Ask about ${_focusedIdea!.title}',
                    allowAttachments: false,
                    onSend: (text, _, _) =>
                        _sendMessage(getBetterViewModel, chatViewModel, text),
                    onStop: chatViewModel.stopGeneration,
                    extraBottomPadding:
                        MediaQuery.viewInsetsOf(context).bottom > 0 ? 4 : 14,
                  ),
                ],
              );
            },
          ),
        ),
      ),
    );
  }
}

class _SheetHeader extends StatelessWidget {
  final bool showingChat;
  final VoidCallback onClose;
  final VoidCallback onShowIdeas;

  const _SheetHeader({
    required this.showingChat,
    required this.onClose,
    required this.onShowIdeas,
  });

  @override
  Widget build(BuildContext context) {
    return GestureDetector(
      behavior: HitTestBehavior.opaque,
      onVerticalDragEnd: (details) {
        if ((details.primaryVelocity ?? 0) > 450) onClose();
      },
      child: Padding(
        padding: const EdgeInsets.fromLTRB(16, 8, 16, 8),
        child: Column(
          children: [
            Container(
              width: 48,
              height: 5,
              decoration: BoxDecoration(
                color: AppColors.textPrimary.withValues(alpha: 0.16),
                borderRadius: BorderRadius.circular(999),
              ),
            ),
            const SizedBox(height: 8),
            ConstrainedBox(
              constraints: const BoxConstraints(maxWidth: 840),
              child: Row(
                children: [
                  Expanded(
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        const Text(
                          'Get Better',
                          style: TextStyle(
                            fontFamily: 'Outfit',
                            color: AppColors.textPrimary,
                            fontSize: 25,
                            fontWeight: FontWeight.w700,
                            letterSpacing: -0.7,
                          ),
                        ),
                        Text(
                          showingChat
                              ? 'Thinking it through with Buddy'
                              : 'Ideas that meet you where you are',
                          style: const TextStyle(
                            color: AppColors.textTertiary,
                            fontSize: 12,
                          ),
                        ),
                      ],
                    ),
                  ),
                  if (showingChat) ...[
                    TextButton.icon(
                      onPressed: onShowIdeas,
                      icon: const Icon(Icons.grid_view_rounded, size: 16),
                      label: const Text('Ideas'),
                    ),
                    const SizedBox(width: 4),
                  ],
                  GlassIconButton(
                    icon: Icons.close_rounded,
                    onTap: onClose,
                    iconSize: 22,
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

class _DiscoveryBody extends StatelessWidget {
  final GetBetterViewModel viewModel;
  final ValueChanged<GetBetterIdea> onOpenIdea;

  const _DiscoveryBody({
    super.key,
    required this.viewModel,
    required this.onOpenIdea,
  });

  @override
  Widget build(BuildContext context) {
    final feed = viewModel.feed;
    if (feed == null) {
      return Center(
        child: Padding(
          padding: const EdgeInsets.all(28),
          child: Text(
            viewModel.notice ??
                'The story library is not available yet. Please try again.',
            textAlign: TextAlign.center,
            style: const TextStyle(
              color: AppColors.textSecondary,
              fontSize: 15,
              height: 1.5,
            ),
          ),
        ),
      );
    }

    return LayoutBuilder(
      builder: (context, constraints) {
        final wide = constraints.maxWidth >= 700;
        final horizontalPadding = wide ? 32.0 : 18.0;

        return ListView(
          padding: EdgeInsets.fromLTRB(
            horizontalPadding,
            8,
            horizontalPadding,
            24,
          ),
          children: [
            Center(
              child: ConstrainedBox(
                constraints: const BoxConstraints(maxWidth: 840),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    _BannerCard(
                      idea: feed.banner,
                      imageFile: viewModel.cachedImages[feed.banner.imageKey],
                      onTap: () => onOpenIdea(feed.banner),
                    ),
                    const SizedBox(height: 22),
                    Text(
                      feed.headline,
                      style: TextStyle(
                        fontFamily: 'CormorantGaramond',
                        color: AppColors.textPrimary,
                        fontSize: wide ? 36 : 31,
                        height: 1.05,
                        fontWeight: FontWeight.w300,
                        letterSpacing: -0.7,
                      ),
                    ),
                    const SizedBox(height: 8),
                    Text(
                      feed.intro,
                      style: const TextStyle(
                        color: AppColors.textSecondary,
                        fontSize: 14,
                        height: 1.55,
                      ),
                    ),
                    const SizedBox(height: 22),
                    Row(
                      children: [
                        const Expanded(
                          child: Text(
                            'Pick a story',
                            style: TextStyle(
                              color: AppColors.textPrimary,
                              fontSize: 18,
                              fontWeight: FontWeight.w700,
                              letterSpacing: -0.3,
                            ),
                          ),
                        ),
                        Text(
                          '${feed.allStories.length} ideas',
                          style: TextStyle(
                            color: AppColors.textPrimary.withValues(
                              alpha: 0.46,
                            ),
                            fontSize: 12,
                          ),
                        ),
                      ],
                    ),
                    const SizedBox(height: 12),
                    _StoryMosaic(
                      stories: feed.ideas,
                      cachedImages: viewModel.cachedImages,
                      onOpenIdea: onOpenIdea,
                    ),
                    if (viewModel.notice != null) ...[
                      const SizedBox(height: 18),
                      Text(
                        viewModel.notice!,
                        textAlign: TextAlign.center,
                        style: const TextStyle(
                          color: AppColors.textTertiary,
                          fontSize: 12,
                          height: 1.4,
                        ),
                      ),
                    ],
                  ],
                ),
              ),
            ),
          ],
        );
      },
    );
  }
}

class _BannerCard extends StatelessWidget {
  final GetBetterIdea idea;
  final File? imageFile;
  final VoidCallback onTap;

  const _BannerCard({
    required this.idea,
    required this.imageFile,
    required this.onTap,
  });

  @override
  Widget build(BuildContext context) {
    return Semantics(
      button: true,
      label: '${idea.title}. Open details.',
      child: GestureDetector(
        onTap: onTap,
        child: AspectRatio(
          aspectRatio: 16 / 9,
          child: ClipRRect(
            borderRadius: BorderRadius.circular(28),
            child: Stack(
              fit: StackFit.expand,
              children: [
                _EditorialImage(imageKey: idea.imageKey, imageFile: imageFile),
                const DecoratedBox(
                  decoration: BoxDecoration(
                    gradient: LinearGradient(
                      begin: Alignment.topCenter,
                      end: Alignment.bottomCenter,
                      colors: [
                        Color(0x12000000),
                        Color(0x24000000),
                        Color(0xCC151712),
                      ],
                      stops: [0, 0.48, 1],
                    ),
                  ),
                ),
                Positioned(
                  left: 22,
                  right: 22,
                  bottom: 20,
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      _IdeaLabel(
                        category: idea.category,
                        personalized: idea.personalized,
                        dark: true,
                      ),
                      const SizedBox(height: 9),
                      Text(
                        idea.title,
                        maxLines: 2,
                        overflow: TextOverflow.ellipsis,
                        style: const TextStyle(
                          fontFamily: 'CormorantGaramond',
                          color: Colors.white,
                          fontSize: 34,
                          height: 0.98,
                          fontWeight: FontWeight.w300,
                          letterSpacing: -0.7,
                        ),
                      ),
                    ],
                  ),
                ),
                Positioned(
                  top: 16,
                  right: 16,
                  child: Container(
                    width: 38,
                    height: 38,
                    decoration: BoxDecoration(
                      color: Colors.black.withValues(alpha: 0.28),
                      shape: BoxShape.circle,
                    ),
                    child: const Icon(
                      Icons.arrow_outward_rounded,
                      color: Colors.white,
                      size: 19,
                    ),
                  ),
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}

class _IdeaCard extends StatelessWidget {
  final GetBetterIdea idea;
  final File? imageFile;
  final VoidCallback onTap;

  const _IdeaCard({
    required this.idea,
    required this.imageFile,
    required this.onTap,
  });

  @override
  Widget build(BuildContext context) {
    return Semantics(
      button: true,
      label: '${idea.title}. Open details.',
      child: GestureDetector(
        onTap: onTap,
        child: FauxGlassCard(
          borderRadius: 22,
          padding: EdgeInsets.zero,
          child: ClipRRect(
            borderRadius: BorderRadius.circular(21),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                SizedBox(
                  height: 154,
                  width: double.infinity,
                  child: _EditorialImage(
                    imageKey: idea.imageKey,
                    imageFile: imageFile,
                  ),
                ),
                Expanded(
                  child: Padding(
                    padding: const EdgeInsets.fromLTRB(16, 14, 16, 14),
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        _IdeaLabel(
                          category: idea.category,
                          personalized: idea.personalized,
                        ),
                        const SizedBox(height: 8),
                        Text(
                          idea.title,
                          maxLines: 2,
                          overflow: TextOverflow.ellipsis,
                          style: const TextStyle(
                            color: AppColors.textPrimary,
                            fontSize: 17,
                            height: 1.15,
                            fontWeight: FontWeight.w700,
                            letterSpacing: -0.3,
                          ),
                        ),
                        const Spacer(),
                        Row(
                          children: [
                            Icon(
                              Icons.schedule_rounded,
                              size: 13,
                              color: AppColors.textPrimary.withValues(
                                alpha: 0.44,
                              ),
                            ),
                            const SizedBox(width: 5),
                            Text(
                              '${idea.minutes} min',
                              style: const TextStyle(
                                color: AppColors.textTertiary,
                                fontSize: 11,
                              ),
                            ),
                            const Spacer(),
                            const Icon(
                              Icons.arrow_forward_rounded,
                              color: AppColors.accent,
                              size: 18,
                            ),
                          ],
                        ),
                      ],
                    ),
                  ),
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}

class _IdeaLabel extends StatelessWidget {
  final String category;
  final bool personalized;
  final bool dark;

  const _IdeaLabel({
    required this.category,
    required this.personalized,
    this.dark = false,
  });

  @override
  Widget build(BuildContext context) {
    final foreground = dark ? Colors.white : AppColors.accent;
    return Wrap(
      spacing: 6,
      runSpacing: 4,
      children: [
        Container(
          padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
          decoration: BoxDecoration(
            color: dark
                ? Colors.white.withValues(alpha: 0.18)
                : AppColors.accent.withValues(alpha: 0.11),
            borderRadius: BorderRadius.circular(999),
          ),
          child: Text(
            category.toUpperCase(),
            style: TextStyle(
              color: foreground,
              fontSize: 9,
              fontWeight: FontWeight.w700,
              letterSpacing: 0.6,
            ),
          ),
        ),
        if (personalized)
          Container(
            padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
            decoration: BoxDecoration(
              color: dark
                  ? Colors.white.withValues(alpha: 0.18)
                  : AppColors.textPrimary.withValues(alpha: 0.06),
              borderRadius: BorderRadius.circular(999),
            ),
            child: Text(
              'PICKED FOR YOU',
              style: TextStyle(
                color: dark ? Colors.white : AppColors.textSecondary,
                fontSize: 9,
                fontWeight: FontWeight.w700,
                letterSpacing: 0.5,
              ),
            ),
          ),
      ],
    );
  }
}

class _StoryMosaic extends StatelessWidget {
  final List<GetBetterIdea> stories;
  final Map<String, File> cachedImages;
  final ValueChanged<GetBetterIdea> onOpenIdea;

  const _StoryMosaic({
    required this.stories,
    required this.cachedImages,
    required this.onOpenIdea,
  });

  @override
  Widget build(BuildContext context) {
    final rows = <Widget>[];
    for (var index = 0; index < stories.length;) {
      final story = stories[index];
      final isWide =
          story.cardType == 'wide' ||
          story.cardType == 'prompt' ||
          story.cardType == 'challenge';
      if (isWide) {
        rows.add(
          _WideStoryCard(
            idea: story,
            imageFile: cachedImages[story.imageKey],
            onTap: () => onOpenIdea(story),
          ),
        );
        index += 1;
      } else {
        final second = index + 1 < stories.length ? stories[index + 1] : null;
        rows.add(
          Row(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Expanded(
                child: SizedBox(
                  height: 286,
                  child: _IdeaCard(
                    idea: story,
                    imageFile: cachedImages[story.imageKey],
                    onTap: () => onOpenIdea(story),
                  ),
                ),
              ),
              const SizedBox(width: 12),
              Expanded(
                child: second == null
                    ? const SizedBox.shrink()
                    : SizedBox(
                        height: 286,
                        child: _IdeaCard(
                          idea: second,
                          imageFile: cachedImages[second.imageKey],
                          onTap: () => onOpenIdea(second),
                        ),
                      ),
              ),
            ],
          ),
        );
        index += 2;
      }
      if (index < stories.length) rows.add(const SizedBox(height: 12));
    }
    return Column(children: rows);
  }
}

class _WideStoryCard extends StatelessWidget {
  final GetBetterIdea idea;
  final File? imageFile;
  final VoidCallback onTap;

  const _WideStoryCard({
    required this.idea,
    required this.imageFile,
    required this.onTap,
  });

  @override
  Widget build(BuildContext context) {
    return Semantics(
      button: true,
      label: '${idea.title}. Open story.',
      child: GestureDetector(
        onTap: onTap,
        child: FauxGlassCard(
          borderRadius: 22,
          padding: const EdgeInsets.all(12),
          child: SizedBox(
            height: 156,
            child: Row(
              children: [
                Expanded(
                  flex: 5,
                  child: Padding(
                    padding: const EdgeInsets.fromLTRB(8, 10, 12, 8),
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        _IdeaLabel(
                          category: idea.category,
                          personalized: false,
                        ),
                        const Spacer(),
                        Text(
                          idea.title,
                          maxLines: 3,
                          overflow: TextOverflow.ellipsis,
                          style: const TextStyle(
                            fontFamily: 'CormorantGaramond',
                            color: AppColors.textPrimary,
                            fontSize: 26,
                            height: 1,
                            fontWeight: FontWeight.w500,
                          ),
                        ),
                      ],
                    ),
                  ),
                ),
                Expanded(
                  flex: 4,
                  child: ClipRRect(
                    borderRadius: BorderRadius.circular(16),
                    child: SizedBox.expand(
                      child: _EditorialImage(
                        imageKey: idea.imageKey,
                        imageFile: imageFile,
                      ),
                    ),
                  ),
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}

class _IdeaDetailSheet extends StatelessWidget {
  final GetBetterIdea idea;
  final File? imageFile;
  final List<GetBetterIdea> relatedStories;
  final Map<String, File> cachedImages;
  final GetBetterStoryState state;
  final VoidCallback onToggleSaved;
  final VoidCallback onToggleCompleted;
  final VoidCallback onShare;
  final ValueChanged<GetBetterIdea> onOpenRelated;
  final VoidCallback onTalk;

  const _IdeaDetailSheet({
    required this.idea,
    required this.imageFile,
    required this.relatedStories,
    required this.cachedImages,
    required this.state,
    required this.onToggleSaved,
    required this.onToggleCompleted,
    required this.onShare,
    required this.onOpenRelated,
    required this.onTalk,
  });

  @override
  Widget build(BuildContext context) {
    return DraggableScrollableSheet(
      expand: false,
      initialChildSize: 0.9,
      minChildSize: 0.55,
      maxChildSize: 0.96,
      snap: true,
      builder: (context, scrollController) {
        return Material(
          color: AppColors.background,
          borderRadius: const BorderRadius.vertical(top: Radius.circular(30)),
          clipBehavior: Clip.antiAlias,
          child: Column(
            children: [
              Padding(
                padding: const EdgeInsets.fromLTRB(18, 10, 10, 8),
                child: Row(
                  children: [
                    Container(
                      width: 44,
                      height: 5,
                      decoration: BoxDecoration(
                        color: AppColors.textPrimary.withValues(alpha: 0.16),
                        borderRadius: BorderRadius.circular(999),
                      ),
                    ),
                    const Spacer(),
                    IconButton(
                      tooltip: 'Close',
                      onPressed: () => Navigator.of(context).pop(),
                      icon: const Icon(Icons.close_rounded),
                    ),
                  ],
                ),
              ),
              Expanded(
                child: ListView(
                  controller: scrollController,
                  padding: const EdgeInsets.fromLTRB(20, 0, 20, 28),
                  children: [
                    Center(
                      child: ConstrainedBox(
                        constraints: const BoxConstraints(maxWidth: 720),
                        child: Column(
                          crossAxisAlignment: CrossAxisAlignment.start,
                          children: [
                            ClipRRect(
                              borderRadius: BorderRadius.circular(24),
                              child: AspectRatio(
                                aspectRatio: 16 / 9,
                                child: _EditorialImage(
                                  imageKey: idea.imageKey,
                                  imageFile: imageFile,
                                ),
                              ),
                            ),
                            const SizedBox(height: 20),
                            _IdeaLabel(
                              category: idea.category,
                              personalized: idea.personalized,
                            ),
                            const SizedBox(height: 12),
                            Text(
                              idea.title,
                              style: const TextStyle(
                                fontFamily: 'CormorantGaramond',
                                color: AppColors.textPrimary,
                                fontSize: 38,
                                height: 1,
                                fontWeight: FontWeight.w300,
                                letterSpacing: -0.8,
                              ),
                            ),
                            const SizedBox(height: 14),
                            Text(
                              idea.narrative,
                              style: const TextStyle(
                                color: AppColors.textSecondary,
                                fontSize: 16,
                                height: 1.65,
                              ),
                            ),
                            const SizedBox(height: 22),
                            FauxGlassCard.section(
                              child: Column(
                                crossAxisAlignment: CrossAxisAlignment.start,
                                children: [
                                  const Text(
                                    'What this means',
                                    style: TextStyle(
                                      color: AppColors.textPrimary,
                                      fontSize: 14,
                                      fontWeight: FontWeight.w700,
                                    ),
                                  ),
                                  const SizedBox(height: 7),
                                  Text(
                                    idea.whatItMeans,
                                    style: const TextStyle(
                                      color: AppColors.textSecondary,
                                      fontSize: 14,
                                      height: 1.5,
                                    ),
                                  ),
                                ],
                              ),
                            ),
                            const SizedBox(height: 24),
                            const Text(
                              'Try this',
                              style: TextStyle(
                                color: AppColors.textPrimary,
                                fontSize: 18,
                                fontWeight: FontWeight.w700,
                                letterSpacing: -0.3,
                              ),
                            ),
                            const SizedBox(height: 12),
                            FauxGlassCard.section(
                              child: Text(
                                idea.tryThis,
                                style: const TextStyle(
                                  color: AppColors.textSecondary,
                                  fontSize: 15,
                                  height: 1.55,
                                ),
                              ),
                            ),
                            if (idea.steps.isNotEmpty) ...[
                              const SizedBox(height: 24),
                              const Text(
                                'Small steps',
                                style: TextStyle(
                                  color: AppColors.textPrimary,
                                  fontSize: 18,
                                  fontWeight: FontWeight.w700,
                                ),
                              ),
                              const SizedBox(height: 12),
                              for (
                                var index = 0;
                                index < idea.steps.length;
                                index++
                              )
                                _StepRow(
                                  number: index + 1,
                                  text: idea.steps[index],
                                ),
                            ],
                            if (relatedStories.isNotEmpty) ...[
                              const SizedBox(height: 24),
                              const Text(
                                'Keep exploring',
                                style: TextStyle(
                                  color: AppColors.textPrimary,
                                  fontSize: 18,
                                  fontWeight: FontWeight.w700,
                                ),
                              ),
                              const SizedBox(height: 12),
                              SizedBox(
                                height: 184,
                                child: ListView.separated(
                                  scrollDirection: Axis.horizontal,
                                  physics: const BouncingScrollPhysics(),
                                  itemCount: relatedStories.length,
                                  separatorBuilder: (_, _) =>
                                      const SizedBox(width: 12),
                                  itemBuilder: (context, index) {
                                    final related = relatedStories[index];
                                    return _RelatedStoryCard(
                                      story: related,
                                      imageFile: cachedImages[related.imageKey],
                                      onTap: () => onOpenRelated(related),
                                    );
                                  },
                                ),
                              ),
                            ],
                            const SizedBox(height: 22),
                            Row(
                              children: [
                                Expanded(
                                  child: OutlinedButton.icon(
                                    onPressed: onToggleSaved,
                                    icon: Icon(
                                      state.saved
                                          ? Icons.bookmark_rounded
                                          : Icons.bookmark_border_rounded,
                                    ),
                                    label: Text(state.saved ? 'Saved' : 'Save'),
                                  ),
                                ),
                                const SizedBox(width: 10),
                                Expanded(
                                  child: OutlinedButton.icon(
                                    onPressed: onToggleCompleted,
                                    icon: Icon(
                                      state.completed
                                          ? Icons.check_circle_rounded
                                          : Icons.check_circle_outline_rounded,
                                    ),
                                    label: Text(
                                      state.completed ? 'Done' : 'Mark done',
                                    ),
                                  ),
                                ),
                              ],
                            ),
                            const SizedBox(height: 10),
                            SizedBox(
                              width: double.infinity,
                              child: OutlinedButton.icon(
                                onPressed: onShare,
                                icon: const Icon(Icons.ios_share_rounded),
                                label: const Text('Copy story to share'),
                              ),
                            ),
                            const SizedBox(height: 22),
                            SizedBox(
                              width: double.infinity,
                              child: FilledButton.icon(
                                onPressed: onTalk,
                                icon: const Icon(
                                  Icons.forum_outlined,
                                  size: 19,
                                ),
                                label: const Text(
                                  'Talk this through with Buddy',
                                ),
                                style: FilledButton.styleFrom(
                                  backgroundColor: AppColors.textPrimary,
                                  foregroundColor: Colors.white,
                                  padding: const EdgeInsets.symmetric(
                                    vertical: 16,
                                  ),
                                  shape: RoundedRectangleBorder(
                                    borderRadius: BorderRadius.circular(18),
                                  ),
                                ),
                              ),
                            ),
                          ],
                        ),
                      ),
                    ),
                  ],
                ),
              ),
            ],
          ),
        );
      },
    );
  }
}

class _StepRow extends StatelessWidget {
  final int number;
  final String text;

  const _StepRow({required this.number, required this.text});

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 12),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Container(
            width: 28,
            height: 28,
            alignment: Alignment.center,
            decoration: BoxDecoration(
              color: AppColors.accent.withValues(alpha: 0.12),
              shape: BoxShape.circle,
            ),
            child: Text(
              '$number',
              style: const TextStyle(
                color: AppColors.accent,
                fontSize: 12,
                fontWeight: FontWeight.w700,
              ),
            ),
          ),
          const SizedBox(width: 12),
          Expanded(
            child: Padding(
              padding: const EdgeInsets.only(top: 3),
              child: Text(
                text,
                style: const TextStyle(
                  color: AppColors.textSecondary,
                  fontSize: 14,
                  height: 1.45,
                ),
              ),
            ),
          ),
        ],
      ),
    );
  }
}

class _RelatedStoryCard extends StatelessWidget {
  final GetBetterIdea story;
  final File? imageFile;
  final VoidCallback onTap;

  const _RelatedStoryCard({
    required this.story,
    required this.imageFile,
    required this.onTap,
  });

  @override
  Widget build(BuildContext context) {
    return Semantics(
      button: true,
      label: '${story.title}. Open related story.',
      child: GestureDetector(
        onTap: onTap,
        child: SizedBox(
          width: 210,
          child: FauxGlassCard(
            borderRadius: 18,
            padding: EdgeInsets.zero,
            child: ClipRRect(
              borderRadius: BorderRadius.circular(17),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  SizedBox(
                    height: 102,
                    width: double.infinity,
                    child: _EditorialImage(
                      imageKey: story.imageKey,
                      imageFile: imageFile,
                    ),
                  ),
                  Expanded(
                    child: Padding(
                      padding: const EdgeInsets.all(12),
                      child: Text(
                        story.title,
                        maxLines: 2,
                        overflow: TextOverflow.ellipsis,
                        style: const TextStyle(
                          color: AppColors.textPrimary,
                          fontSize: 14,
                          height: 1.15,
                          fontWeight: FontWeight.w700,
                        ),
                      ),
                    ),
                  ),
                ],
              ),
            ),
          ),
        ),
      ),
    );
  }
}

class _EditorialImage extends StatelessWidget {
  final String imageKey;
  final File? imageFile;

  const _EditorialImage({required this.imageKey, required this.imageFile});

  static const _palettes = <String, List<Color>>{
    'momentum': [Color(0xFF43645A), Color(0xFFDCA77A)],
    'focus': [Color(0xFF334B6B), Color(0xFFD9B96E)],
    'calm': [Color(0xFF6E8C7A), Color(0xFFE7D3B2)],
    'learning': [Color(0xFF5A4D75), Color(0xFFE39A77)],
    'wellbeing': [Color(0xFF6D8051), Color(0xFFF0B86C)],
    'relationships': [Color(0xFF8A4F5C), Color(0xFFE9A36A)],
    'career': [Color(0xFF405D68), Color(0xFFD3A874)],
    'creativity': [Color(0xFF755481), Color(0xFFE8A653)],
    'money': [Color(0xFF3E6A56), Color(0xFFD7C477)],
    'routines': [Color(0xFF466771), Color(0xFFE0B58C)],
    'confidence': [Color(0xFF8C5E48), Color(0xFFE6BF73)],
    'adventure': [Color(0xFF476A78), Color(0xFFE4A86A)],
  };

  @override
  Widget build(BuildContext context) {
    final colors = _palettes[imageKey] ?? _palettes['momentum']!;
    final fallback = DecoratedBox(
      decoration: BoxDecoration(
        gradient: LinearGradient(
          begin: Alignment.topLeft,
          end: Alignment.bottomRight,
          colors: colors,
        ),
      ),
      child: Center(
        child: Icon(
          _iconForKey(imageKey),
          color: Colors.white.withValues(alpha: 0.72),
          size: 52,
        ),
      ),
    );
    if (imageFile == null) return fallback;
    return Image.file(
      imageFile!,
      fit: BoxFit.cover,
      filterQuality: FilterQuality.medium,
      errorBuilder: (_, _, _) => fallback,
    );
  }

  static IconData _iconForKey(String key) {
    return switch (key) {
      'focus' => Icons.center_focus_strong_rounded,
      'calm' => Icons.air_rounded,
      'learning' => Icons.menu_book_rounded,
      'wellbeing' => Icons.spa_outlined,
      'relationships' => Icons.favorite_border_rounded,
      'career' => Icons.work_outline_rounded,
      'creativity' => Icons.palette_outlined,
      'money' => Icons.savings_outlined,
      'routines' => Icons.wb_sunny_outlined,
      'confidence' => Icons.auto_awesome_rounded,
      'adventure' => Icons.explore_outlined,
      _ => Icons.trending_up_rounded,
    };
  }
}
