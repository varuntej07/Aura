import 'dart:convert';

import 'package:drift/native.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:aura/data/local/app_database.dart';
import 'package:aura/data/models/get_better_feed.dart';
import 'package:aura/data/repositories/get_better_repository.dart';
import 'package:aura/data/services/backend_api_service.dart';

void main() {
  late AppDatabase database;
  late _RecordingBackend backend;
  late GetBetterRepository repository;

  setUp(() {
    database = AppDatabase.forTesting(NativeDatabase.memory());
    backend = _RecordingBackend();
    repository = GetBetterRepository(
      database: database,
      backendApiService: backend,
    );
  });

  tearDown(() => database.close());

  test('fresh disk catalog is returned without a backend call', () async {
    final feed = _feed();
    final now = DateTime.now();
    await database
        .into(database.getBetterCatalogCaches)
        .insert(
          GetBetterCatalogCachesCompanion.insert(
            cacheKey: GetBetterRepository.catalogCacheKey,
            catalogVersion: feed.catalogVersion,
            feedJson: jsonEncode(feed.toJson()),
            checkedAt: now,
            updatedAt: now,
          ),
        );

    final result = await repository.loadCatalog();

    expect(result.feed?.catalogVersion, feed.catalogVersion);
    expect(result.usingCachedCatalog, isTrue);
    expect(backend.syncCalls, 0);
  });

  test(
    'missing catalog is fetched once and persisted as one snapshot',
    () async {
      final feed = _feed();
      backend.syncResult = GetBetterCatalogSync(
        notModified: false,
        catalogVersion: feed.catalogVersion,
        feed: feed,
      );

      final result = await repository.loadCatalog();
      final cachedRows = await database
          .select(database.getBetterCatalogCaches)
          .get();

      expect(result.feed?.catalogVersion, feed.catalogVersion);
      expect(backend.syncCalls, 1);
      expect(cachedRows, hasLength(1));
      expect(cachedRows.single.catalogVersion, feed.catalogVersion);
    },
  );

  test('activity updates local progress and flushes one batch', () async {
    final story = _story();

    await repository.recordActivity(
      userId: 'user-1',
      story: story,
      eventType: 'opened',
    );
    await repository.recordActivity(
      userId: 'user-1',
      story: story,
      eventType: 'saved',
    );

    final states = await repository.loadStoryStates('user-1');
    expect(states[story.id]?.saved, isTrue);
    expect(await repository.flushActivity('user-1'), isTrue);
    expect(backend.batchCalls, 1);
    expect(backend.lastEvents, hasLength(2));
    expect(await database.select(database.getBetterEventOutbox).get(), isEmpty);
  });

  test('failed activity batch remains in the durable outbox', () async {
    backend.acceptBatch = false;
    final story = _story();
    await repository.recordActivity(
      userId: 'user-1',
      story: story,
      eventType: 'completed',
    );

    expect(await repository.flushActivity('user-1'), isFalse);
    expect(
      await database.select(database.getBetterEventOutbox).get(),
      hasLength(1),
    );
  });
}

class _RecordingBackend extends Fake implements BackendApiService {
  int syncCalls = 0;
  int batchCalls = 0;
  bool acceptBatch = true;
  GetBetterCatalogSync? syncResult;
  List<GetBetterActivityEvent> lastEvents = const [];

  @override
  Future<GetBetterCatalogSync?> syncGetBetterCatalog({
    String? knownCatalogVersion,
  }) async {
    syncCalls += 1;
    return syncResult;
  }

  @override
  Future<bool> sendGetBetterActivityBatch({
    required String batchId,
    required List<GetBetterActivityEvent> events,
  }) async {
    batchCalls += 1;
    lastEvents = events;
    return acceptBatch;
  }
}

GetBetterFeed _feed() {
  final featured = _story(featured: true, cardType: 'hero');
  return GetBetterFeed(
    headline: 'Pick a story',
    intro: 'Small stories for ordinary problems.',
    banner: featured,
    ideas: [_story(id: 'second-story')],
    nextCursor: 0,
    generatedAt: DateTime.utc(2026, 7, 26),
    catalogVersion: 'test.v1',
  );
}

GetBetterIdea _story({
  String id = 'story-one',
  bool featured = false,
  String cardType = 'square',
}) {
  return GetBetterIdea(
    id: id,
    title: 'Take one small step',
    category: 'Focus',
    summary: 'A small step can make a hard job feel possible.',
    whyItFits: 'Starting is often the hardest part.',
    steps: const ['Choose one tiny action.'],
    chatPrompt: 'Help me choose one small step.',
    imageKey: 'focus',
    personalized: false,
    minutes: 5,
    storyVersion: 1,
    narrative: 'Sam had a big job. He chose one small piece and began there.',
    whatItMeans: 'You do not need to finish everything at once.',
    tryThis: 'Pick one action that takes less than five minutes.',
    relatedStoryIds: const [],
    cardType: cardType,
    displayOrder: 1,
    featured: featured,
  );
}
