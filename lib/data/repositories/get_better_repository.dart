import 'dart:convert';

import 'package:drift/drift.dart';
import 'package:uuid/uuid.dart';

import '../../core/logging/app_logger.dart';
import '../local/app_database.dart';
import '../models/get_better_feed.dart';
import '../services/backend_api_service.dart';

class GetBetterCatalogLoad {
  final GetBetterFeed? feed;
  final bool refreshed;
  final bool usingCachedCatalog;

  const GetBetterCatalogLoad({
    required this.feed,
    required this.refreshed,
    required this.usingCachedCatalog,
  });
}

class GetBetterCatalogSnapshot {
  final GetBetterFeed feed;
  final String catalogVersion;
  final DateTime checkedAt;

  const GetBetterCatalogSnapshot({
    required this.feed,
    required this.catalogVersion,
    required this.checkedAt,
  });
}

class GetBetterStoryState {
  final bool saved;
  final bool completed;

  const GetBetterStoryState({required this.saved, required this.completed});
}

/// Cache-first access to canonical Get Better stories and private progress.
///
/// Story content is stored as one versioned JSON snapshot because the catalog
/// is fetched and replaced atomically. This avoids row-by-row writes and keeps
/// schema changes in the story document independent from Drift migrations.
class GetBetterRepository {
  static const catalogCacheKey = 'published';
  static const catalogRevalidationInterval = Duration(hours: 24);
  static const _uuid = Uuid();
  static const _outboxBatchLimit = 50;

  final AppDatabase _database;
  final BackendApiService _backendApiService;

  const GetBetterRepository({
    required AppDatabase database,
    required BackendApiService backendApiService,
  }) : _database = database,
       _backendApiService = backendApiService;

  Future<GetBetterCatalogSnapshot?> loadCachedCatalog() async {
    final cached = await (_database.select(
      _database.getBetterCatalogCaches,
    )..where((row) => row.cacheKey.equals(catalogCacheKey))).getSingleOrNull();
    if (cached == null) return null;
    try {
      final decoded = jsonDecode(cached.feedJson);
      if (decoded is! Map<String, dynamic>) {
        throw const FormatException('Cached Get Better feed is not an object');
      }
      final feed = GetBetterFeed.fromJson(decoded);
      if (feed.catalogVersion != cached.catalogVersion) {
        throw const FormatException(
          'Cached Get Better catalog version does not match its payload',
        );
      }
      return GetBetterCatalogSnapshot(
        feed: feed,
        catalogVersion: cached.catalogVersion,
        checkedAt: cached.checkedAt,
      );
    } catch (error, stackTrace) {
      AppLogger.warning(
        'Get Better catalog cache could not be decoded',
        tag: 'GetBetterRepository',
        metadata: {'error': error.toString()},
      );
      AppLogger.debug(stackTrace.toString(), tag: 'GetBetterRepository');
      return null;
    }
  }

  Future<GetBetterFeed?> loadCachedFeed() async {
    return (await loadCachedCatalog())?.feed;
  }

  Future<GetBetterCatalogLoad> loadCatalog({
    GetBetterCatalogSnapshot? cachedCatalog,
  }) async {
    final snapshot = cachedCatalog ?? await loadCachedCatalog();
    final cachedFeed = snapshot?.feed;
    final cacheIsFresh =
        snapshot != null &&
        DateTime.now().difference(snapshot.checkedAt) <
            catalogRevalidationInterval;
    if (cachedFeed != null && cacheIsFresh) {
      return GetBetterCatalogLoad(
        feed: cachedFeed,
        refreshed: false,
        usingCachedCatalog: true,
      );
    }

    final sync = await _backendApiService.syncGetBetterCatalog(
      knownCatalogVersion: snapshot?.catalogVersion,
    );
    if (sync == null) {
      return GetBetterCatalogLoad(
        feed: cachedFeed,
        refreshed: false,
        usingCachedCatalog: cachedFeed != null,
      );
    }

    final now = DateTime.now();
    if (sync.notModified && cachedFeed != null) {
      await (_database.update(_database.getBetterCatalogCaches)
            ..where((row) => row.cacheKey.equals(catalogCacheKey)))
          .write(GetBetterCatalogCachesCompanion(checkedAt: Value(now)));
      return GetBetterCatalogLoad(
        feed: cachedFeed,
        refreshed: true,
        usingCachedCatalog: true,
      );
    }

    final freshFeed = sync.feed;
    if (freshFeed == null ||
        freshFeed.catalogVersion.isEmpty ||
        freshFeed.catalogVersion != sync.catalogVersion) {
      AppLogger.warning(
        'Get Better catalog sync returned an incomplete payload',
        tag: 'GetBetterRepository',
      );
      return GetBetterCatalogLoad(
        feed: cachedFeed,
        refreshed: false,
        usingCachedCatalog: cachedFeed != null,
      );
    }

    await _database
        .into(_database.getBetterCatalogCaches)
        .insertOnConflictUpdate(
          GetBetterCatalogCachesCompanion.insert(
            cacheKey: catalogCacheKey,
            catalogVersion: freshFeed.catalogVersion,
            feedJson: jsonEncode(freshFeed.toJson()),
            checkedAt: now,
            updatedAt: now,
          ),
        );
    return GetBetterCatalogLoad(
      feed: freshFeed,
      refreshed: true,
      usingCachedCatalog: false,
    );
  }

  Future<Map<String, GetBetterStoryState>> loadStoryStates(
    String userId,
  ) async {
    final rows = await (_database.select(
      _database.getBetterStoryProgress,
    )..where((row) => row.userId.equals(userId))).get();
    return {
      for (final row in rows)
        row.storyId: GetBetterStoryState(
          saved: row.saved,
          completed: row.completed,
        ),
    };
  }

  Future<void> recordActivity({
    required String userId,
    required GetBetterIdea story,
    required String eventType,
  }) async {
    final occurredAt = DateTime.now().toUtc();
    final eventId = _uuid.v4();
    await _database.transaction(() async {
      final existing =
          await (_database.select(_database.getBetterStoryProgress)
                ..where((row) => row.userId.equals(userId))
                ..where((row) => row.storyId.equals(story.id)))
              .getSingleOrNull();
      var opened = existing?.opened ?? false;
      var saved = existing?.saved ?? false;
      var completed = existing?.completed ?? false;
      var lastOpenedAt = existing?.lastOpenedAt;
      switch (eventType) {
        case 'opened':
        case 'related_opened':
          opened = true;
          lastOpenedAt = occurredAt;
          break;
        case 'saved':
          saved = true;
          break;
        case 'unsaved':
          saved = false;
          break;
        case 'completed':
          completed = true;
          break;
        case 'uncompleted':
          completed = false;
          break;
        case 'shared':
        case 'buddy_chat_started':
          break;
        default:
          throw ArgumentError.value(
            eventType,
            'eventType',
            'Unsupported Get Better activity type',
          );
      }

      await _database
          .into(_database.getBetterStoryProgress)
          .insertOnConflictUpdate(
            GetBetterStoryProgressCompanion.insert(
              userId: userId,
              storyId: story.id,
              storyVersion: Value(story.storyVersion),
              opened: Value(opened),
              saved: Value(saved),
              completed: Value(completed),
              lastOpenedAt: Value(lastOpenedAt),
              updatedAt: Value(occurredAt),
            ),
          );
      await _database
          .into(_database.getBetterEventOutbox)
          .insert(
            GetBetterEventOutboxCompanion.insert(
              eventId: eventId,
              userId: userId,
              eventType: eventType,
              storyId: story.id,
              storyVersion: story.storyVersion,
              occurredAt: occurredAt,
            ),
          );
    });
  }

  Future<bool> flushActivity(String userId) async {
    final pending =
        await (_database.select(_database.getBetterEventOutbox)
              ..where((row) => row.userId.equals(userId))
              ..orderBy([(row) => OrderingTerm.asc(row.occurredAt)])
              ..limit(_outboxBatchLimit))
            .get();
    if (pending.isEmpty) return true;

    final events = [
      for (final row in pending)
        GetBetterActivityEvent(
          eventId: row.eventId,
          eventType: row.eventType,
          storyId: row.storyId,
          storyVersion: row.storyVersion,
          occurredAt: row.occurredAt,
        ),
    ];
    final batchId =
        'gb.${pending.first.eventId}.${pending.last.eventId}.${pending.length}';
    final accepted = await _backendApiService.sendGetBetterActivityBatch(
      batchId: batchId,
      events: events,
    );
    if (!accepted) return false;

    final eventIds = pending.map((row) => row.eventId).toList(growable: false);
    await (_database.delete(
      _database.getBetterEventOutbox,
    )..where((row) => row.eventId.isIn(eventIds))).go();
    return true;
  }
}
