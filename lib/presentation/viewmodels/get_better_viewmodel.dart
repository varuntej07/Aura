import 'dart:async';
import 'dart:io';

import '../../core/base/safe_change_notifier.dart';
import '../../data/models/get_better_feed.dart';
import '../../data/repositories/get_better_repository.dart';
import '../../data/services/get_better_image_cache.dart';

class GetBetterViewModel extends SafeChangeNotifier {
  final GetBetterRepository _repository;
  final GetBetterImageCache _imageCache;

  GetBetterFeed? _feed;
  Map<String, File> _cachedImages = const {};
  Map<String, GetBetterStoryState> _storyStates = const {};
  final Set<String> _loadingImageKeys = {};
  bool _loading = true;
  String? _notice;
  String? _loadedForUserId;

  GetBetterViewModel({
    required GetBetterRepository repository,
    required GetBetterImageCache imageCache,
  }) : _repository = repository,
       _imageCache = imageCache;

  GetBetterFeed? get feed => _feed;
  Map<String, File> get cachedImages => _cachedImages;
  bool get loading => _loading;
  String? get notice => _notice;

  Future<void> load(String userId) async {
    if (_loadedForUserId == userId) return;
    _loadedForUserId = userId;
    _loading = true;
    _notice = null;
    safeNotifyListeners();

    final cachedCatalog = await _repository.loadCachedCatalog();
    final cachedFeed = cachedCatalog?.feed;
    if (cachedFeed != null) {
      _feed = cachedFeed;
      _loading = false;
      unawaited(_warmImages(cachedFeed));
      safeNotifyListeners();
    }

    final result = await _repository.loadCatalog(cachedCatalog: cachedCatalog);
    _storyStates = await _repository.loadStoryStates(userId);
    _loading = false;
    if (result.feed != null) {
      _feed = result.feed;
      unawaited(_warmImages(result.feed!));
    } else {
      _notice = cachedFeed == null
          ? "Buddy couldn't load the story library yet. Try again when you're back online."
          : 'You are seeing the saved story library while Buddy reconnects.';
    }
    unawaited(_repository.flushActivity(userId));
    safeNotifyListeners();
  }

  GetBetterStoryState storyState(String storyId) {
    return _storyStates[storyId] ??
        const GetBetterStoryState(saved: false, completed: false);
  }

  List<GetBetterIdea> relatedStories(GetBetterIdea story) {
    final currentFeed = _feed;
    if (currentFeed == null) return const [];
    return story.relatedStoryIds
        .map(currentFeed.storyById)
        .whereType<GetBetterIdea>()
        .toList(growable: false);
  }

  Future<void> recordOpened(
    GetBetterIdea story, {
    bool fromRelatedStory = false,
  }) async {
    final userId = _loadedForUserId;
    if (userId == null) return;
    await _repository.recordActivity(
      userId: userId,
      story: story,
      eventType: fromRelatedStory ? 'related_opened' : 'opened',
    );
  }

  Future<void> toggleSaved(GetBetterIdea story) async {
    final userId = _loadedForUserId;
    if (userId == null) return;
    final current = storyState(story.id);
    final next = GetBetterStoryState(
      saved: !current.saved,
      completed: current.completed,
    );
    _storyStates = {..._storyStates, story.id: next};
    safeNotifyListeners();
    await _repository.recordActivity(
      userId: userId,
      story: story,
      eventType: next.saved ? 'saved' : 'unsaved',
    );
  }

  Future<void> toggleCompleted(GetBetterIdea story) async {
    final userId = _loadedForUserId;
    if (userId == null) return;
    final current = storyState(story.id);
    final next = GetBetterStoryState(
      saved: current.saved,
      completed: !current.completed,
    );
    _storyStates = {..._storyStates, story.id: next};
    safeNotifyListeners();
    await _repository.recordActivity(
      userId: userId,
      story: story,
      eventType: next.completed ? 'completed' : 'uncompleted',
    );
  }

  Future<void> recordShared(GetBetterIdea story) async {
    final userId = _loadedForUserId;
    if (userId == null) return;
    await _repository.recordActivity(
      userId: userId,
      story: story,
      eventType: 'shared',
    );
  }

  Future<void> recordBuddyChatStarted(GetBetterIdea story) async {
    final userId = _loadedForUserId;
    if (userId == null) return;
    await _repository.recordActivity(
      userId: userId,
      story: story,
      eventType: 'buddy_chat_started',
    );
  }

  String conversationContext({GetBetterIdea? focusedIdea}) {
    final buffer = StringBuffer(
      "I opened the shared Get Better story library. Let's talk about these "
      'stories like thinking partners, without turning them into generic advice.\n',
    );
    if (focusedIdea != null) {
      buffer
        ..writeln('\nThe story is **${focusedIdea.title}**.')
        ..writeln(focusedIdea.narrative)
        ..writeln('\nWhat it means: ${focusedIdea.whatItMeans}')
        ..writeln('\nA gentle experiment: ${focusedIdea.tryThis}');
    } else if (_feed case final feed?) {
      buffer.writeln('\nHere is what is currently in the library:');
      for (final idea in feed.allStories.take(8)) {
        buffer.writeln('- **${idea.title}**: ${idea.summary}');
      }
    }
    buffer.write(
      '\nAsk me what feels realistic, what does not fit, or where you want to begin.',
    );
    return buffer.toString();
  }

  Future<void> _warmImages(GetBetterFeed feed) async {
    final keys = {
      feed.banner.imageKey,
      ...feed.ideas.map((idea) => idea.imageKey),
    };
    for (final key in keys) {
      if (isDisposed) return;
      if (_cachedImages.containsKey(key) || !_loadingImageKeys.add(key)) {
        continue;
      }
      final file = await _imageCache.load(key);
      _loadingImageKeys.remove(key);
      if (file == null || isDisposed) continue;
      _cachedImages = {..._cachedImages, key: file};
      safeNotifyListeners();
    }
  }

  @override
  void dispose() {
    final userId = _loadedForUserId;
    if (userId != null) {
      unawaited(_repository.flushActivity(userId));
    }
    super.dispose();
  }
}
