/// What is already running for the user, shown as the status pills above the deck.
///
/// Everything here is tolerant of missing fields: the pills are a glance, and a
/// backend that has not shipped a section yet should cost one pill, never the row.
class HomeStatus {
  final bool briefingReady;
  final int remindersToday;
  final List<HomeStatusRitual> rituals;
  final List<HomeStatusTracker> trackers;

  /// How many rituals are active, and how many the server will keep. Separate
  /// from [rituals], which is capped at three for the pill row and so can never
  /// prove the user is at the limit.
  ///
  /// Both default to 0, which reads as "unknown" rather than "at the cap": an
  /// older backend does not send these, and that must not lock the ritual card.
  final int ritualsActive;
  final int ritualsMax;

  const HomeStatus({
    this.briefingReady = false,
    this.remindersToday = 0,
    this.rituals = const [],
    this.trackers = const [],
    this.ritualsActive = 0,
    this.ritualsMax = 0,
  });

  static const HomeStatus empty = HomeStatus();

  bool get isEmpty =>
      !briefingReady &&
      remindersToday == 0 &&
      rituals.isEmpty &&
      trackers.isEmpty;

  factory HomeStatus.fromJson(Map<String, dynamic> json) {
    final rawRituals = json['rituals'];
    final rawTrackers = json['trackers'];
    return HomeStatus(
      briefingReady: json['briefing_ready'] == true,
      remindersToday:
          json['reminders_today'] is int ? json['reminders_today'] as int : 0,
      rituals: rawRituals is List
          ? rawRituals
              .map(HomeStatusRitual.fromJson)
              .whereType<HomeStatusRitual>()
              .toList()
          : const [],
      trackers: rawTrackers is List
          ? rawTrackers
              .map(HomeStatusTracker.fromJson)
              .whereType<HomeStatusTracker>()
              .toList()
          : const [],
      ritualsActive:
          json['rituals_active'] is int ? json['rituals_active'] as int : 0,
      ritualsMax: json['rituals_max'] is int ? json['rituals_max'] as int : 0,
    );
  }

  Map<String, dynamic> toJson() => {
        'briefing_ready': briefingReady,
        'reminders_today': remindersToday,
        'rituals': rituals.map((r) => r.toJson()).toList(),
        'trackers': trackers.map((t) => t.toJson()).toList(),
        'rituals_active': ritualsActive,
        'rituals_max': ritualsMax,
      };
}

class HomeStatusRitual {
  final String ritualId;
  final String title;

  /// Local wall time of the next occurrence, as the server resolved it.
  final String nextFireLocal;

  const HomeStatusRitual({
    required this.ritualId,
    required this.title,
    this.nextFireLocal = '',
  });

  /// The short form for a pill: "Joke at 8:00 AM".
  String get pillLabel {
    final clock = _clockOf(nextFireLocal);
    final subject = title.split(' ').take(2).join(' ');
    return clock.isEmpty ? title : '$subject at $clock';
  }

  static String _clockOf(String isoLocal) {
    if (isoLocal.isEmpty) return '';
    final parsed = DateTime.tryParse(isoLocal);
    if (parsed == null) return '';
    final hour = parsed.hour % 12 == 0 ? 12 : parsed.hour % 12;
    final minute = parsed.minute.toString().padLeft(2, '0');
    return '$hour:$minute ${parsed.hour < 12 ? 'AM' : 'PM'}';
  }

  static HomeStatusRitual? fromJson(Object? raw) {
    if (raw is! Map) return null;
    final id = raw['ritual_id'];
    final title = raw['title'];
    if (id is! String || id.isEmpty || title is! String || title.isEmpty) {
      return null;
    }
    return HomeStatusRitual(
      ritualId: id,
      title: title,
      nextFireLocal:
          raw['next_fire_local'] is String ? raw['next_fire_local'] as String : '',
    );
  }

  Map<String, dynamic> toJson() => {
        'ritual_id': ritualId,
        'title': title,
        'next_fire_local': nextFireLocal,
      };
}

class HomeStatusTracker {
  final String trackerId;
  final String title;

  const HomeStatusTracker({required this.trackerId, required this.title});

  static HomeStatusTracker? fromJson(Object? raw) {
    if (raw is! Map) return null;
    final id = raw['tracker_id'];
    final title = raw['title'];
    if (id is! String || id.isEmpty || title is! String || title.isEmpty) {
      return null;
    }
    return HomeStatusTracker(trackerId: id, title: title);
  }

  Map<String, dynamic> toJson() => {
        'tracker_id': trackerId,
        'title': title,
      };
}
