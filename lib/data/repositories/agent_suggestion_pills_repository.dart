import '../models/home_deck_card.dart';
import '../services/firestore_service.dart';

/// Hardcoded fallback pills shown when Firestore hasn't been populated yet
/// (before the first 7 AM scheduled run) or when the fetch fails.
const Map<String, List<String>> _fallbackSuggestionPillsByAgentId = {
  'sports': [
    'IPL scores today',
    'Top performer today',
    'Tournament standings',
    'Match highlights',
    'Player stats',
  ],
  'technews': [
    'Latest AI news',
    'Top tech story',
    'This week in LLMs',
    'Startup funding',
    'Open source picks',
  ],
  'posts': [
    'Draft a tweet',
    'LinkedIn post idea',
    'Thread starter',
    'Trending angle',
    'Contrarian take',
  ],
  // Main Buddy chat. Generic-but-warm starters shown for brand-new users, before
  // the first personalized generation lands, or on a fetch failure. Written in the
  // user's own voice — tapping one drops it into the input box, so each must read
  // as something the user types TO Buddy, never a question Buddy asks the user.
  'buddy': [
    'Help me plan my day',
    'I need to vent for a sec',
    'Keep motivating me today ',
  ],
};

/// Reads agent suggestion pills from `agent_suggestion_pills/{uid}` in Firestore.
/// Always returns a non-empty list — falls back to hardcoded defaults on any error
/// so the UI always has something to show.
class AgentSuggestionPillsRepository {
  final FirestoreService _firestoreService;

  const AgentSuggestionPillsRepository({required FirestoreService firestoreService})
      : _firestoreService = firestoreService;

  Future<List<String>> fetchSuggestionPillsForAgent(
    String uid,
    String agentId,
  ) async {
    final result = await _firestoreService.getDocument(
      'agent_suggestion_pills',
      uid,
      (data) => data,
    );
    return result.when(
      success: (data) {
        final raw = data[agentId];
        if (raw is List && raw.isNotEmpty) return raw.cast<String>();
        return _fallbackPillsForAgent(agentId);
      },
      failure: (_) => _fallbackPillsForAgent(agentId),
    );
  }

  List<String> _fallbackPillsForAgent(String agentId) =>
      _fallbackSuggestionPillsByAgentId[agentId] ?? [];

  /// Reads the home deck from the same document the pills come from.
  ///
  /// Same doc, one read: the deck and the pills are generated together, so
  /// fetching them separately would cost a second round-trip for bytes already
  /// in hand. Returns the fallback deck on any failure, an empty key, or a
  /// payload whose cards all failed to parse.
  /// A successful read with nothing generated yet and a failed read are NOT the
  /// same situation, and v1 treating them alike is what made a fresh install look
  /// empty-handed. A read that succeeded proves the device is online, so it gets
  /// the starter deck — real ritual cards that can actually be created. Only a
  /// failed read falls back to cards that need no network to be honoured.
  Future<List<HomeDeckCard>> fetchHomeDeck(String uid) async {
    final result = await _firestoreService.getDocument(
      'agent_suggestion_pills',
      uid,
      (data) => data,
    );
    return result.when(
      success: (data) {
        final cards = HomeDeckCard.listFromJson(data['home_deck']);
        return cards.isNotEmpty ? cards : starterHomeDeck;
      },
      failure: (_) => fallbackHomeDeck,
    );
  }
}

/// The briefing slot. Static: its route is fixed and there is nothing about it a
/// model could usefully personalise, so it costs no generation tokens.
const HomeDeckCard kBriefingCard = HomeDeckCard(
  kind: HomeDeckCardKind.briefing,
  title: "Today's briefing",
  subtitle: 'What I picked up for you while you were away.',
  primaryLabel: 'Read it',
  action: HomeDeckAction(
    type: HomeDeckActionType.navigate,
    route: '/briefing',
  ),
);

/// The connectors slot. Static for the same reason as [kBriefingCard].
///
/// The face runs through the catalog one logo at a time. The order is not
/// cosmetic: Google Calendar and Notion are the connectors that can actually be
/// linked today, so they come first and are what the card shows on the frames a
/// user is most likely to see. Everything after them is marked "Coming soon" on
/// the connectors screen this card opens, which is the one thing that keeps the
/// slideshow honest. Keep this list in step with connectors_screen.dart, and if
/// a name ever appears here that the destination does not list at all, it is a
/// promise nothing can keep.
const HomeDeckCard kConnectCard = HomeDeckCard(
  kind: HomeDeckCardKind.connect,
  title: 'Connect your world',
  // Names no single product: the face is cycling seven of them, and a subtitle
  // that says "calendar and mail" under the word "Spotify" reads as a mistake.
  subtitle: 'The apps you already live in, wired into me.',
  primaryLabel: 'Connect',
  suggestions: [
    HomeDeckSuggestion(
      id: 'gcal',
      text: 'Google Calendar',
      iconAsset: 'assets/icons/google_calendar.png',
    ),
    HomeDeckSuggestion(
      id: 'notion',
      text: 'Notion',
      iconAsset: 'assets/icons/notion.png',
    ),
    HomeDeckSuggestion(
      id: 'gmail',
      text: 'Gmail',
      iconAsset: 'assets/icons/gmail.png',
    ),
    HomeDeckSuggestion(
      id: 'todoist',
      text: 'Todoist',
      iconAsset: 'assets/icons/todoist.png',
    ),
    HomeDeckSuggestion(
      id: 'slack',
      text: 'Slack',
      iconAsset: 'assets/icons/slack.png',
    ),
    HomeDeckSuggestion(
      id: 'spotify',
      text: 'Spotify',
      iconAsset: 'assets/icons/spotify.png',
    ),
    HomeDeckSuggestion(
      id: 'oura',
      text: 'Oura',
      iconAsset: 'assets/icons/oura.png',
    ),
  ],
  action: HomeDeckAction(
    type: HomeDeckActionType.navigate,
    route: '/settings/connectors',
  ),
);

/// The ritual slot before the first generation lands. Its phrases are the ones
/// worth offering someone Buddy knows nothing about yet.
const HomeDeckCard starterRitualCard = HomeDeckCard(
  kind: HomeDeckCardKind.ritual,
  title: 'A joke every morning',
  subtitle: 'Something small to wake up to.',
  primaryLabel: 'Set it up',
  suggestions: [
    HomeDeckSuggestion(
      id: 'joke',
      text: 'A joke every morning',
      contentKind: 'joke',
    ),
    HomeDeckSuggestion(
      id: 'best',
      text: 'Best part of today?',
      contentKind: 'question',
    ),
    HomeDeckSuggestion(
      id: 'push',
      text: 'A push when I need it',
      contentKind: 'motivation',
    ),
    HomeDeckSuggestion(
      id: 'checkin',
      text: 'Just check in on me',
      contentKind: 'checkin',
    ),
  ],
  options: [
    HomeDeckOption(
      id: 'd0800',
      label: 'Everyday 8AM',
      schedule: RitualSchedule(freq: 'daily', hour: 8, minute: 0),
    ),
    HomeDeckOption(
      id: 'd2130',
      label: 'Everyday 9:30PM',
      schedule: RitualSchedule(freq: 'daily', hour: 21, minute: 30),
    ),
    HomeDeckOption(
      id: 'w0800',
      label: 'Weekdays 8AM',
      schedule: RitualSchedule(
        freq: 'weekly',
        hour: 8,
        minute: 0,
        weekdays: [0, 1, 2, 3, 4],
      ),
    ),
  ],
  action: HomeDeckAction(
    type: HomeDeckActionType.ritualCreate,
    contentKind: 'joke',
    defaultOptionId: 'd0800',
  ),
);

/// The reminder slot before the first generation lands. Generic on purpose: with
/// no context yet, the honest offer is the ordinary thing people put off.
const HomeDeckCard starterRemindCard = HomeDeckCard(
  kind: HomeDeckCardKind.remind,
  title: 'Stretch break this afternoon',
  subtitle: 'One minute, away from the screen.',
  primaryLabel: 'Remind me',
  suggestions: [
    HomeDeckSuggestion(id: 'stretch', text: 'Stretch break this afternoon'),
    HomeDeckSuggestion(id: 'water', text: 'Drink some water'),
    HomeDeckSuggestion(id: 'msg', text: 'Message someone I owe a reply'),
    HomeDeckSuggestion(id: 'walk', text: 'Get outside for ten minutes'),
  ],
  options: [
    HomeDeckOption(
      id: 'r1500',
      label: 'Today 3PM',
      localTime: ReminderLocalTime(hour: 15, minute: 0),
    ),
    HomeDeckOption(
      id: 'r1730',
      label: 'Today 5:30PM',
      localTime: ReminderLocalTime(hour: 17, minute: 30),
    ),
    HomeDeckOption(
      id: 'r0900',
      label: 'Tomorrow 9AM',
      localTime: ReminderLocalTime(hour: 9, minute: 0, dayOffset: 1),
    ),
  ],
  action: HomeDeckAction(
    type: HomeDeckActionType.reminderCreate,
    message: 'Stretch break. Stand up, shoulders back, one minute.',
    defaultOptionId: 'r1500',
  ),
);

/// Shown to an account with no generated deck yet: a new user, or one whose first
/// generation has not run. Online by definition, so these create real things.
///
/// Written by hand rather than generated because the first thing someone sees
/// should not depend on a model call having already happened for them.
final List<HomeDeckCard> starterHomeDeck = List.unmodifiable([
  starterRitualCard,
  starterRemindCard,
  kBriefingCard,
  kConnectCard,
]);

/// Shown when the fetch itself failed.
///
/// The ritual and reminder slots are permanent fixtures now, so unlike the old
/// offline deck this one DOES contain cards whose confirm needs the server. That
/// is the deliberate trade: a hand missing two of its four slots reads as the app
/// being broken, and a confirm that cannot reach Buddy already says so in the
/// open card (see HomeDeckViewModel._copyFor).
final List<HomeDeckCard> fallbackHomeDeck = starterHomeDeck;

