/// One card in the home deck: a single thing Buddy offers, doable in one tap.
///
/// Every `fromJson` here returns null rather than throwing, and an unrecognised
/// `kind` or `action.type` is one of the things that returns null. That is the
/// forward-compatibility contract: the backend can ship a new kind of card to
/// everyone tomorrow and installs that have never heard of it will quietly show
/// one card fewer instead of crashing on the home screen.
library;

enum HomeDeckCardKind { ritual, remind, track, talk, briefing, connect }

enum HomeDeckActionType {
  ritualCreate,
  reminderCreate,
  trackerCreate,
  chatOpen,
  navigate,
}

const Map<String, HomeDeckCardKind> _kindByWire = {
  'ritual': HomeDeckCardKind.ritual,
  'remind': HomeDeckCardKind.remind,
  'track': HomeDeckCardKind.track,
  'talk': HomeDeckCardKind.talk,
  'briefing': HomeDeckCardKind.briefing,
  'connect': HomeDeckCardKind.connect,
};

const Map<String, HomeDeckActionType> _actionByWire = {
  'ritual_create': HomeDeckActionType.ritualCreate,
  'reminder_create': HomeDeckActionType.reminderCreate,
  'tracker_create': HomeDeckActionType.trackerCreate,
  'chat_open': HomeDeckActionType.chatOpen,
  'navigate': HomeDeckActionType.navigate,
};

String _wireOfKind(HomeDeckCardKind kind) =>
    _kindByWire.entries.firstWhere((e) => e.value == kind).key;

String _wireOfAction(HomeDeckActionType type) =>
    _actionByWire.entries.firstWhere((e) => e.value == type).key;

/// A recurring schedule, already structured by the time it reaches the phone.
/// Weekdays are 0=Monday..6=Sunday, matching the server and DateTime.weekday-1.
class RitualSchedule {
  final String freq;
  final int hour;
  final int minute;
  final List<int> weekdays;

  const RitualSchedule({
    required this.freq,
    required this.hour,
    required this.minute,
    this.weekdays = const [],
  });

  static RitualSchedule? fromJson(Object? raw) {
    if (raw is! Map) return null;
    final freq = raw['freq'];
    final hour = raw['hour'];
    final minute = raw['minute'];
    if (freq is! String || hour is! int || minute is! int) return null;
    if (freq != 'daily' && freq != 'weekly') return null;
    if (hour < 0 || hour > 23 || minute < 0 || minute > 59) return null;
    final days = (raw['weekdays'] is List)
        ? (raw['weekdays'] as List).whereType<int>().where((d) => d >= 0 && d <= 6).toList()
        : <int>[];
    if (freq == 'weekly' && days.isEmpty) return null;
    return RitualSchedule(freq: freq, hour: hour, minute: minute, weekdays: days);
  }

  Map<String, dynamic> toJson() => {
        'freq': freq,
        'hour': hour,
        'minute': minute,
        'weekdays': weekdays,
      };
}

/// A one-shot clock time the user picked from chips.
class ReminderLocalTime {
  final int hour;
  final int minute;
  final int dayOffset;

  const ReminderLocalTime({
    required this.hour,
    required this.minute,
    this.dayOffset = 0,
  });

  static ReminderLocalTime? fromJson(Object? raw) {
    if (raw is! Map) return null;
    final hour = raw['hour'];
    final minute = raw['minute'];
    if (hour is! int || minute is! int) return null;
    if (hour < 0 || hour > 23 || minute < 0 || minute > 59) return null;
    final offset = raw['day_offset'];
    return ReminderLocalTime(
      hour: hour,
      minute: minute,
      dayOffset: (offset is int && offset >= 0 && offset <= 7) ? offset : 0,
    );
  }

  Map<String, dynamic> toJson() => {
        'hour': hour,
        'minute': minute,
        'day_offset': dayOffset,
      };
}

/// One choice on the back of a card: a time, a cadence, a variant.
class HomeDeckOption {
  final String id;
  final String label;
  final RitualSchedule? schedule;
  final ReminderLocalTime? localTime;

  const HomeDeckOption({
    required this.id,
    required this.label,
    this.schedule,
    this.localTime,
  });

  /// What the chip actually shows: "Everyday 8AM", "Weekdays 8AM", "Today 3PM".
  ///
  /// Built from the structured schedule rather than from [label], because the
  /// label is a free string written by the model and by decks generated before
  /// this format existed ("Every day · 8:00 AM", "10:00 am"). Formatting from the
  /// data is the only way the chips read the same on every card, including decks
  /// already sitting in Firestore. [label] is the fallback for an option that
  /// somehow carries no time at all.
  String get displayLabel {
    final schedule = this.schedule;
    if (schedule != null) {
      final when = _clock(schedule.hour, schedule.minute);
      if (schedule.freq == 'weekly') {
        return '${_days(schedule.weekdays)} $when';
      }
      return 'Everyday $when';
    }
    final localTime = this.localTime;
    if (localTime != null) {
      final when = _clock(localTime.hour, localTime.minute);
      switch (localTime.dayOffset) {
        case 0:
          return 'Today $when';
        case 1:
          return 'Tomorrow $when';
        default:
          return 'In ${localTime.dayOffset} days $when';
      }
    }
    return label;
  }

  /// "8AM", "9:30PM". No minutes on the hour, no leading zero, no space.
  static String _clock(int hour, int minute) {
    final twelve = hour % 12 == 0 ? 12 : hour % 12;
    final suffix = hour < 12 ? 'AM' : 'PM';
    return minute == 0
        ? '$twelve$suffix'
        : '$twelve:${minute.toString().padLeft(2, '0')}$suffix';
  }

  /// 0 = Monday, matching the server and DateTime.weekday - 1.
  static String _days(List<int> weekdays) {
    const names = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
    final days = weekdays.toSet();
    if (days.length == 7) return 'Everyday';
    if (days.length == 5 && days.every((d) => d <= 4)) return 'Weekdays';
    if (days.length == 2 && days.every((d) => d >= 5)) return 'Weekends';
    final sorted = days.toList()..sort();
    return sorted.map((d) => names[d.clamp(0, 6)]).join(', ');
  }

  static HomeDeckOption? fromJson(Object? raw) {
    if (raw is! Map) return null;
    final id = raw['id'];
    final label = raw['label'];
    if (id is! String || id.isEmpty || label is! String || label.isEmpty) {
      return null;
    }
    return HomeDeckOption(
      id: id,
      label: label,
      schedule: RitualSchedule.fromJson(raw['schedule']),
      localTime: ReminderLocalTime.fromJson(raw['local_time']),
    );
  }

  Map<String, dynamic> toJson() => {
        'id': id,
        'label': label,
        if (schedule != null) 'schedule': schedule!.toJson(),
        if (localTime != null) 'local_time': localTime!.toJson(),
      };
}

/// One phrase the card face flashes, and the thing confirming it creates.
///
/// Carries no time of its own by design: the card's [HomeDeckOption]s remain the
/// only source of truth for when something fires, so the face can never advertise
/// a time the confirm button would not set.
class HomeDeckSuggestion {
  final String id;
  final String text;

  /// Ritual only: what Buddy writes when it fires (joke, motivation, checkin,
  /// question). Empty on a reminder, where the text IS the message.
  final String contentKind;

  /// Optional logo shown beside the phrase. Lives here, next to the text it
  /// belongs to, so a renamed phrase cannot drift away from its icon the way a
  /// lookup table keyed on display text would. Client-set only: the server has no
  /// bundled assets to point at.
  final String iconAsset;

  const HomeDeckSuggestion({
    required this.id,
    required this.text,
    this.contentKind = '',
    this.iconAsset = '',
  });

  static HomeDeckSuggestion? fromJson(Object? raw) {
    if (raw is! Map) return null;
    final id = raw['id'];
    final text = raw['text'];
    if (id is! String || id.isEmpty || text is! String || text.isEmpty) {
      return null;
    }
    return HomeDeckSuggestion(
      id: id,
      text: text,
      contentKind: raw['content_kind'] is String ? raw['content_kind'] as String : '',
    );
  }

  Map<String, dynamic> toJson() => {
        'id': id,
        'text': text,
        'content_kind': contentKind,
      };
}

/// What confirming the card actually does.
class HomeDeckAction {
  final HomeDeckActionType type;
  final String contentKind;
  final String message;
  final String request;
  final String openingMessage;
  final String route;
  final String defaultOptionId;

  const HomeDeckAction({
    required this.type,
    this.contentKind = '',
    this.message = '',
    this.request = '',
    this.openingMessage = '',
    this.route = '',
    this.defaultOptionId = '',
  });

  static HomeDeckAction? fromJson(Object? raw) {
    if (raw is! Map) return null;
    final type = _actionByWire[raw['type']];
    if (type == null) return null;
    String str(String key) {
      final value = raw[key];
      return value is String ? value : '';
    }

    return HomeDeckAction(
      type: type,
      contentKind: str('content_kind'),
      message: str('message'),
      request: str('request'),
      openingMessage: str('opening_message'),
      route: str('route'),
      defaultOptionId: str('default_option_id'),
    );
  }

  Map<String, dynamic> toJson() => {
        'type': _wireOfAction(type),
        'content_kind': contentKind,
        'message': message,
        'request': request,
        'opening_message': openingMessage,
        'route': route,
        'default_option_id': defaultOptionId,
      };
}

class HomeDeckCard {
  final HomeDeckCardKind kind;
  final String title;
  final String subtitle;
  final String primaryLabel;
  final List<HomeDeckOption> options;

  /// The phrases the face flashes. Never empty once parsed: a card carrying none
  /// falls back to a single suggestion built from [title].
  final List<HomeDeckSuggestion> suggestions;
  final HomeDeckAction action;

  const HomeDeckCard({
    required this.kind,
    required this.title,
    required this.action,
    this.subtitle = '',
    this.primaryLabel = '',
    this.options = const [],
    this.suggestions = const [],
  });

  /// The phrases to flash, with the title standing in for a card that has none.
  /// Const decks declare no suggestions, so this is what the UI reads.
  List<HomeDeckSuggestion> get phrases => suggestions.isNotEmpty
      ? suggestions
      : [
          HomeDeckSuggestion(
            id: 'title',
            text: title,
            contentKind: action.contentKind,
          ),
        ];

  /// A stable identity for the deck's contents, used to decide whether a fetched
  /// deck differs from the one already in hand. Titles alone are not enough: a
  /// card can keep its title while every phrase behind it rotates.
  String get signature =>
      '${_wireOfKind(kind)}|$title|${suggestions.map((s) => '${s.id}~${s.text}').join(',')}';

  /// True when confirming this card needs the network, and therefore when it must
  /// not appear in an offline fallback deck.
  bool get createsSomething =>
      action.type == HomeDeckActionType.ritualCreate ||
      action.type == HomeDeckActionType.reminderCreate ||
      action.type == HomeDeckActionType.trackerCreate;

  /// The option the back face opens on, or null for a card with no choices.
  HomeDeckOption? get defaultOption {
    if (options.isEmpty) return null;
    for (final option in options) {
      if (option.id == action.defaultOptionId) return option;
    }
    return options.first;
  }

  static HomeDeckCard? fromJson(Object? raw) {
    if (raw is! Map) return null;
    final kind = _kindByWire[raw['kind']];
    final title = raw['title'];
    if (kind == null || title is! String || title.isEmpty) return null;
    final action = HomeDeckAction.fromJson(raw['action']);
    if (action == null) return null;

    final options = <HomeDeckOption>[];
    final rawOptions = raw['options'];
    if (rawOptions is List) {
      for (final entry in rawOptions) {
        final option = HomeDeckOption.fromJson(entry);
        if (option != null) options.add(option);
      }
    }
    // A card whose choices all failed to parse cannot be confirmed into anything
    // real, so it is dropped rather than shown with an empty back face.
    if (kind == HomeDeckCardKind.ritual && options.every((o) => o.schedule == null)) {
      return null;
    }
    if (kind == HomeDeckCardKind.remind && options.every((o) => o.localTime == null)) {
      return null;
    }

    // Absent on every deck generated before the flashing faces shipped. Those
    // documents are still live in Firestore, so an empty list here is normal and
    // [phrases] falls back to the title.
    final suggestions = <HomeDeckSuggestion>[];
    final rawSuggestions = raw['suggestions'];
    if (rawSuggestions is List) {
      for (final entry in rawSuggestions) {
        final suggestion = HomeDeckSuggestion.fromJson(entry);
        if (suggestion != null) suggestions.add(suggestion);
      }
    }

    return HomeDeckCard(
      kind: kind,
      title: title,
      subtitle: raw['subtitle'] is String ? raw['subtitle'] as String : '',
      primaryLabel:
          raw['primary_label'] is String ? raw['primary_label'] as String : '',
      options: options,
      suggestions: suggestions,
      action: action,
    );
  }

  /// Everything except the named suggestion, used after one is confirmed so the
  /// card stops offering something the user already set up.
  HomeDeckCard withoutSuggestion(String suggestionId) => HomeDeckCard(
        kind: kind,
        title: title,
        subtitle: subtitle,
        primaryLabel: primaryLabel,
        options: options,
        suggestions:
            suggestions.where((s) => s.id != suggestionId).toList(growable: false),
        action: action,
      );

  Map<String, dynamic> toJson() => {
        'kind': _wireOfKind(kind),
        'title': title,
        'subtitle': subtitle,
        'primary_label': primaryLabel,
        'options': options.map((o) => o.toJson()).toList(),
        // Must round-trip: this is the SharedPreferences cache, and dropping the
        // field here would make every card stop flashing on the next cold start
        // until the network read lands.
        'suggestions': suggestions.map((s) => s.toJson()).toList(),
        'action': action.toJson(),
      };

  static List<HomeDeckCard> listFromJson(Object? raw) {
    if (raw is! List) return const [];
    final cards = <HomeDeckCard>[];
    for (final entry in raw) {
      final card = HomeDeckCard.fromJson(entry);
      if (card != null) cards.add(card);
    }
    return cards;
  }
}
