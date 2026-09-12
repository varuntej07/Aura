import 'dart:async';
import 'dart:convert';

import 'package:shared_preferences/shared_preferences.dart';
import 'package:uuid/uuid.dart';

import '../../core/base/safe_change_notifier.dart';
import '../../core/errors/app_exception.dart';
import '../../core/logging/app_logger.dart';
import '../../core/network/api_response.dart';
import '../../data/models/home_deck_card.dart';
import '../../data/models/home_status.dart';
import '../../data/repositories/agent_suggestion_pills_repository.dart';
import '../../data/services/backend_api_service.dart';

/// The hand is exactly four, always, in a fixed order: ritual, reminder,
/// briefing, connectors. More than four stops reading as a hand and starts
/// reading as a feed, which is the thing this screen exists to avoid; fewer
/// reads as something having broken.
const int kHomeDeckVisibleCards = 4;

/// State for the home-screen deck: which cards are in hand, which spares are
/// waiting, and what happened to the one being confirmed.
///
/// Deliberately separate from HomeViewModel, which its own doc comment scopes to
/// live voice-session state. A deck failure must never be able to disturb a call.
class HomeDeckViewModel extends SafeChangeNotifier {
  final AgentSuggestionPillsRepository _repository;
  final BackendApiService _api;
  final Uuid _uuid = const Uuid();

  HomeDeckViewModel({
    required AgentSuggestionPillsRepository repository,
    required BackendApiService api,
  })  : _repository = repository,
        _api = api;

  List<HomeDeckCard> _hand = const [];
  bool _loaded = false;
  String? _uid;
  HomeStatus _status = HomeStatus.empty;

  /// What is already running for this user: today's briefing, reminders due,
  /// rituals and watched topics. Empty until the first fetch resolves.
  HomeStatus get status => _status;

  /// Client-generated ids, one per distinct thing a card can create. The id is
  /// the server document id, so a double-tapped confirm writes the same document
  /// instead of creating a second ritual.
  ///
  /// Keyed on slot + suggestion + option rather than on the slot alone: one slot
  /// now creates several different things over its life, and a slot-only key
  /// would make the second create reuse the first's document id, which the server
  /// answers with `created: false` — a silent no-op reported to the user as
  /// success. Cleared only on install, so re-confirming the identical phrase at
  /// the identical time stays the no-op it should be.
  final Map<String, String> _idempotencyKeys = {};

  List<HomeDeckCard> get hand => _hand;
  bool get loaded => _loaded;

  /// Renders from cache first so the throw can start on the first frame; the
  /// network copy swaps in behind it if it turns out to be different.
  Future<void> load(String uid) async {
    _uid = uid;
    final cached = await _readCache(uid);
    if (cached.isNotEmpty && !_loaded) {
      _install(cached);
    }
    // Pills paint from cache on the first frame too, then correct themselves.
    // A count that is one stale open behind is far better than a row that pops
    // in a second after everything else has settled.
    unawaited(_loadStatus(uid));

    final fetched = await _repository.fetchHomeDeck(uid);
    if (fetched.isNotEmpty && !_sameDeck(fetched, _hand)) {
      _install(fetched);
      await _writeCache(uid, fetched);
    } else if (!_loaded) {
      _install(fetched);
    }
  }

  /// Build the fixed hand: whatever the server generated for the two live slots,
  /// then the two static ones. Always exactly four, even when the fetch failed,
  /// so no caller has to cope with a short hand.
  void _install(List<HomeDeckCard> cards) {
    HomeDeckCard pick(HomeDeckCardKind kind, HomeDeckCard fallback) {
      for (final card in cards) {
        if (card.kind == kind) return card;
      }
      return fallback;
    }

    _hand = List.unmodifiable([
      pick(HomeDeckCardKind.ritual, starterRitualCard),
      pick(HomeDeckCardKind.remind, starterRemindCard),
      kBriefingCard,
      kConnectCard,
    ]);
    _idempotencyKeys.clear();
    _loaded = true;
    safeNotifyListeners();
  }

  /// Compares what is actually on the cards, not just their titles: a card keeps
  /// its title while every phrase behind it rotates, and a title-only comparison
  /// would leave that new deck unseen and uncached.
  bool _sameDeck(List<HomeDeckCard> a, List<HomeDeckCard> b) {
    if (b.isEmpty) return false;
    String signature(List<HomeDeckCard> cards) => cards
        .where((c) =>
            c.kind == HomeDeckCardKind.ritual || c.kind == HomeDeckCardKind.remind)
        .map((c) => c.signature)
        .join('#');
    return signature(a) == signature(b);
  }

  /// The stable document id for one specific thing: this phrase, at this time,
  /// in this slot.
  String idempotencyKeyFor(int index, String suggestionId, String optionId) =>
      _idempotencyKeys.putIfAbsent(
        '$index|$suggestionId|$optionId',
        () => _uuid.v4(),
      );

  /// Drop a phrase the user just acted on, so the card stops offering something
  /// they already set up. The slot itself stays: the four are permanent.
  void consumeSuggestion(int index, String suggestionId) {
    if (index < 0 || index >= _hand.length) return;
    final updated = [..._hand];
    updated[index] = updated[index].withoutSuggestion(suggestionId);
    _hand = List.unmodifiable(updated);
    safeNotifyListeners();
  }

  /// Perform what the card promises. Returns null on success, or the line to
  /// show on the card's back face when it failed.
  ///
  /// The caller must not animate the card away until this resolves successfully:
  /// a card that flies into the orb and then reports an error has already told
  /// the user it worked.
  Future<String?> confirm({
    required int index,
    required HomeDeckCard card,
    required HomeDeckSuggestion suggestion,
    required HomeDeckOption? option,
  }) async {
    final optionId = option?.id ?? card.defaultOption?.id ?? '';
    switch (card.action.type) {
      case HomeDeckActionType.ritualCreate:
        final schedule = option?.schedule ?? card.defaultOption?.schedule;
        if (schedule == null) return "I couldn't read that schedule.";
        return _run(
          () => _api.createRitual(
            clientRitualId: idempotencyKeyFor(index, suggestion.id, optionId),
            // The phrase carries its own kind; the card's is the fallback for a
            // deck generated before phrases existed.
            contentKind: suggestion.contentKind.isNotEmpty
                ? suggestion.contentKind
                : card.action.contentKind,
            // The phrase on the face IS the ritual. Posting card.title here would
            // create whatever the card is named instead of what the user picked.
            title: suggestion.text,
            schedule: schedule.toJson(),
          ),
        );
      case HomeDeckActionType.reminderCreate:
        final localTime = option?.localTime ?? card.defaultOption?.localTime;
        if (localTime == null) return "I couldn't read that time.";
        return _run(
          () => _api.createDeckReminder(
            clientReminderId: idempotencyKeyFor(index, suggestion.id, optionId),
            message: suggestion.text,
            localTime: localTime.toJson(),
          ),
        );
      case HomeDeckActionType.trackerCreate:
        // No longer reachable from the fixed hand, but the wire still permits a
        // tracker card and this switch has to stay exhaustive.
        return _run(() => _api.createTracker(card.action.request));
      case HomeDeckActionType.chatOpen:
      case HomeDeckActionType.navigate:
        // Navigation kinds do their work on the screen, not the wire.
        return null;
    }
  }

  Future<String?> _run(
    Future<Result<Map<String, dynamic>>> Function() call,
  ) async {
    final result = await call();
    return result.when(
      success: (_) => null,
      failure: (error) {
        AppLogger.error(
          'Home deck action failed',
          error: error,
          tag: 'HomeDeckVM',
          metadata: {'code': error.code.name},
        );
        return _copyFor(error);
      },
    );
  }

  /// What the user reads when it did not work. Every line says what to do next,
  /// because the card stays open and retryable.
  String _copyFor(AppException error) {
    switch (error.code) {
      case ErrorCode.networkUnavailable:
      case ErrorCode.requestTimeout:
        return "Couldn't reach Buddy. Try again in a moment.";
      case ErrorCode.unauthorized:
      case ErrorCode.authTokenExpired:
        return 'Sign in again and I can set this up.';
      default:
        // The server writes these for the user (a schedule it could not accept,
        // a missing timezone, the ritual cap), so they are shown verbatim.
        final message = error.message.trim();
        return message.isNotEmpty && message.length <= 140
            ? message
            : "That didn't go through. Try again in a moment.";
    }
  }

  Future<void> _loadStatus(String uid) async {
    final cached = await _readStatusCache(uid);
    if (cached != null && _status.isEmpty) {
      _status = cached;
      safeNotifyListeners();
    }
    final fetched = await _api.fetchHomeStatus();
    if (fetched == null) return;
    _status = fetched;
    safeNotifyListeners();
    await _writeStatusCache(uid, fetched);
  }

  Future<HomeStatus?> _readStatusCache(String uid) async {
    try {
      final prefs = await SharedPreferences.getInstance();
      final raw = prefs.getString(_statusCacheKey(uid));
      if (raw == null || raw.isEmpty) return null;
      final decoded = jsonDecode(raw);
      return decoded is Map<String, dynamic>
          ? HomeStatus.fromJson(decoded)
          : null;
    } catch (_) {
      return null;
    }
  }

  Future<void> _writeStatusCache(String uid, HomeStatus status) async {
    try {
      final prefs = await SharedPreferences.getInstance();
      await prefs.setString(_statusCacheKey(uid), jsonEncode(status.toJson()));
    } catch (_) {
      // One stale open at worst.
    }
  }

  String _statusCacheKey(String uid) => 'home_status_$uid';

  String _cacheKey(String uid) => 'home_deck_$uid';

  Future<List<HomeDeckCard>> _readCache(String uid) async {
    try {
      final prefs = await SharedPreferences.getInstance();
      final raw = prefs.getString(_cacheKey(uid));
      if (raw == null || raw.isEmpty) return const [];
      return HomeDeckCard.listFromJson(jsonDecode(raw));
    } catch (_) {
      // A cache miss is the normal first run; a corrupt one must not be fatal.
      return const [];
    }
  }

  Future<void> _writeCache(String uid, List<HomeDeckCard> cards) async {
    try {
      final prefs = await SharedPreferences.getInstance();
      await prefs.setString(
        _cacheKey(uid),
        jsonEncode(cards.map((c) => c.toJson()).toList()),
      );
    } catch (_) {
      // Losing the cache only costs one round-trip on the next open.
    }
  }

  /// Clears the cached deck for the signed-in user (sign-out, account delete).
  Future<void> clearCache() async {
    final uid = _uid;
    if (uid == null) return;
    try {
      final prefs = await SharedPreferences.getInstance();
      await prefs.remove(_cacheKey(uid));
    } catch (_) {
      // Nothing here is recoverable and nothing here matters enough to surface.
    }
  }
}
