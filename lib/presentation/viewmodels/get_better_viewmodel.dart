import 'dart:async';
import 'dart:io';

import '../../core/base/safe_change_notifier.dart';
import '../../data/models/get_better_feed.dart';
import '../../data/services/backend_api_service.dart';
import '../../data/services/get_better_image_cache.dart';

class GetBetterViewModel extends SafeChangeNotifier {
  final BackendApiService _backendApiService;
  final GetBetterImageCache _imageCache;

  GetBetterFeed _feed = _fallbackFeed(0);
  Map<String, File> _cachedImages = const {};
  final Set<String> _loadingImageKeys = {};
  bool _loading = true;
  bool _loadingMore = false;
  String? _notice;
  String? _loadedForUserId;

  GetBetterViewModel({
    required BackendApiService backendApiService,
    required GetBetterImageCache imageCache,
  }) : _backendApiService = backendApiService,
       _imageCache = imageCache;

  GetBetterFeed get feed => _feed;
  Map<String, File> get cachedImages => _cachedImages;
  bool get loading => _loading;
  bool get loadingMore => _loadingMore;
  String? get notice => _notice;

  Future<void> load(String userId) async {
    if (_loadedForUserId == userId) return;
    _loadedForUserId = userId;
    _loading = true;
    _notice = null;
    safeNotifyListeners();
    unawaited(_warmImages(_feed));

    final fresh = await _backendApiService.fetchGetBetterFeed();
    _loading = false;
    if (fresh != null && fresh.ideas.isNotEmpty) {
      _feed = fresh;
      unawaited(_warmImages(fresh));
    } else {
      _notice =
          "Buddy couldn't personalize this just yet, so these are a few solid places to start.";
    }
    safeNotifyListeners();
  }

  Future<void> loadMore() async {
    if (_loadingMore) return;
    _loadingMore = true;
    _notice = null;
    safeNotifyListeners();

    final fresh = await _backendApiService.fetchGetBetterFeed(
      cursor: _feed.nextCursor,
      excludeIds: [_feed.banner.id, ..._feed.ideas.map((idea) => idea.id)],
    );

    final personalized = _feed.ideas
        .where((idea) => idea.personalized)
        .take(2)
        .toList();
    final discovery = fresh != null && fresh.ideas.isNotEmpty
        ? fresh.ideas
              .where((idea) => !idea.personalized)
              .toList(growable: false)
        : _fallbackFeed(_feed.nextCursor).ideas;

    _feed = _feed.copyWith(
      ideas: [...personalized, ...discovery],
      nextCursor: fresh?.nextCursor ?? _feed.nextCursor + 1,
      generatedAt: DateTime.now(),
    );
    _loadingMore = false;
    if (fresh == null) {
      _notice =
          "Fresh ideas didn't come through, so I shuffled in a few saved ones.";
    }

    unawaited(_warmImages(_feed));
    safeNotifyListeners();
  }

  String conversationContext({GetBetterIdea? focusedIdea}) {
    final buffer = StringBuffer(
      "I pulled together these Get Better ideas for you. Let's talk about them like a "
      'thinking partner, without turning them into generic self-help advice.\n',
    );
    if (focusedIdea != null) {
      buffer
        ..writeln('\nThe idea you opened is **${focusedIdea.title}**.')
        ..writeln(focusedIdea.summary)
        ..writeln('\nWhy it may help: ${focusedIdea.whyItFits}');
      if (focusedIdea.steps.isNotEmpty) {
        buffer.writeln(
          '\nA possible starting point: ${focusedIdea.steps.first}',
        );
      }
    } else {
      buffer.writeln('\nHere is what is currently on the page:');
      for (final idea in [_feed.banner, ..._feed.ideas.take(6)]) {
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

  static GetBetterFeed _fallbackFeed(int page) {
    final batches = <List<Map<String, dynamic>>>[
      [
        {
          'id': 'make_the_next_step_tiny',
          'title': 'Make the next step tiny',
          'category': 'Momentum',
          'summary':
              'Shrink one thing you have been avoiding until it feels almost too easy.',
          'why_it_fits':
              'Starting creates useful information. You can decide what comes next after two honest minutes.',
          'steps': [
            'Name the thing that feels stuck.',
            'Choose an action that takes under two minutes.',
            'Stop after that step unless continuing feels natural.',
          ],
          'chat_prompt': 'Help me make the next step on something feel tiny.',
          'image_key': 'momentum',
          'personalized': false,
          'minutes': 2,
        },
        {
          'id': 'protect_one_focus_window',
          'title': 'Protect one focus window',
          'category': 'Focus',
          'summary':
              'Give one meaningful task a short stretch without incoming noise.',
          'why_it_fits':
              'A protected window is easier to keep than an ambitious all-day productivity plan.',
          'steps': [
            'Pick one outcome for the window.',
            'Silence optional alerts for 20 minutes.',
            'Write down distractions instead of following them.',
          ],
          'chat_prompt': 'Help me choose what deserves one focus window today.',
          'image_key': 'focus',
          'personalized': false,
          'minutes': 20,
        },
        {
          'id': 'send_the_warm_message',
          'title': 'Send the warm message',
          'category': 'Relationships',
          'summary':
              'Reach out to someone you care about without waiting for the perfect reason.',
          'why_it_fits':
              'Small bids for connection often matter more than polished catch-ups.',
          'steps': [
            'Choose one person who crossed your mind.',
            'Say what reminded you of them.',
            'Leave the message easy to answer.',
          ],
          'chat_prompt': 'Help me write a warm, low-pressure message.',
          'image_key': 'relationships',
          'personalized': false,
          'minutes': 5,
        },
        {
          'id': 'reset_the_room',
          'title': 'Reset the room, not your life',
          'category': 'Routines',
          'summary':
              'Make one visible space calmer so the next hour asks less of you.',
          'why_it_fits':
              'A small environmental reset can lower friction without demanding a full routine overhaul.',
          'steps': [
            'Pick the surface you see most.',
            'Remove five things that do not belong.',
            'Place the next useful object within reach.',
          ],
          'chat_prompt': 'Help me pick a quick reset that will actually help.',
          'image_key': 'routines',
          'personalized': false,
          'minutes': 8,
        },
        {
          'id': 'learn_one_layer_deeper',
          'title': 'Learn one layer deeper',
          'category': 'Learning',
          'summary':
              'Turn something interesting into a question you can investigate today.',
          'why_it_fits':
              'A precise question makes curiosity actionable and easier to remember.',
          'steps': [
            'Write the thing you want to understand.',
            'Turn it into one concrete question.',
            'Explain the answer back in three sentences.',
          ],
          'chat_prompt':
              'Help me turn something I am curious about into a good question.',
          'image_key': 'learning',
          'personalized': false,
          'minutes': 15,
        },
        {
          'id': 'make_space_for_calm',
          'title': 'Make space for calm',
          'category': 'Wellbeing',
          'summary':
              'Use a brief sensory reset before deciding what the rest of the day needs.',
          'why_it_fits':
              'A calmer baseline makes the next choice clearer without pretending everything is fixed.',
          'steps': [
            'Put both feet on the floor.',
            'Take five slower breaths than usual.',
            'Name the one need that is loudest right now.',
          ],
          'chat_prompt':
              'Talk me through a quick reset and help me choose what comes next.',
          'image_key': 'calm',
          'personalized': false,
          'minutes': 3,
        },
      ],
      [
        {
          'id': 'money_date_without_judgment',
          'title': 'Have a money date without judgment',
          'category': 'Money',
          'summary':
              'Look at one week of spending with curiosity instead of trying to fix everything.',
          'why_it_fits':
              'A short factual check-in builds awareness without turning money into a shame spiral.',
          'steps': [
            'Open the last seven days of transactions.',
            'Mark one expense that felt worth it.',
            'Pick one easy adjustment for next week.',
          ],
          'chat_prompt': 'Help me do a calm ten-minute money check-in.',
          'image_key': 'money',
          'personalized': false,
          'minutes': 10,
        },
        {
          'id': 'collect_small_wins',
          'title': 'Collect evidence that you can',
          'category': 'Confidence',
          'summary':
              'Notice three recent moments when you handled something better than you expected.',
          'why_it_fits':
              'Confidence grows more reliably from specific evidence than from pep talks.',
          'steps': [
            'List three moments from the last month.',
            'Name what you did in each one.',
            'Choose the strength that repeats.',
          ],
          'chat_prompt':
              'Help me find evidence of what I am getting better at.',
          'image_key': 'confidence',
          'personalized': false,
          'minutes': 7,
        },
        {
          'id': 'make_something_badly',
          'title': 'Make something badly on purpose',
          'category': 'Creativity',
          'summary':
              'Give yourself permission to make a rough first version with no audience.',
          'why_it_fits':
              'Lowering the quality bar makes experimentation possible again.',
          'steps': [
            'Choose a tiny thing to make.',
            'Set a twelve-minute timer.',
            'Finish a rough version before evaluating it.',
          ],
          'chat_prompt': 'Give me a small creative challenge with no pressure.',
          'image_key': 'creativity',
          'personalized': false,
          'minutes': 12,
        },
        {
          'id': 'career_energy_audit',
          'title': 'Follow the work that gives energy',
          'category': 'Career',
          'summary':
              'Separate the work that drains you from the work that leaves you more alive.',
          'why_it_fits':
              'Energy patterns can reveal useful career direction before a grand plan exists.',
          'steps': [
            'List three recent work moments.',
            'Mark which gave or took energy.',
            'Find one ingredient to seek more often.',
          ],
          'chat_prompt': 'Help me run a quick career energy audit.',
          'image_key': 'career',
          'personalized': false,
          'minutes': 10,
        },
        {
          'id': 'plan_a_micro_adventure',
          'title': 'Plan a micro-adventure',
          'category': 'Adventure',
          'summary':
              'Put one unfamiliar place or experience into the next seven days.',
          'why_it_fits':
              'Novelty can make a week feel larger without requiring a major trip.',
          'steps': [
            'Pick a radius you can reach easily.',
            'Find one place you have never entered.',
            'Choose a specific day and time.',
          ],
          'chat_prompt': 'Help me plan a realistic micro-adventure this week.',
          'image_key': 'adventure',
          'personalized': false,
          'minutes': 10,
        },
        {
          'id': 'ask_a_better_question',
          'title': 'Ask yourself a better question',
          'category': 'Clarity',
          'summary':
              'Replace “What should I do?” with a question that exposes the real tradeoff.',
          'why_it_fits':
              'Good questions reduce vague pressure and make choices easier to compare.',
          'steps': [
            'Write the decision in one sentence.',
            'Name what each option protects.',
            'Ask which cost you are more willing to carry.',
          ],
          'chat_prompt': 'Help me find the real question behind a decision.',
          'image_key': 'calm',
          'personalized': false,
          'minutes': 8,
        },
      ],
    ];
    final selected = batches[page % batches.length];
    return GetBetterFeed.fromJson({
      'headline': 'A little better, your way',
      'intro':
          'No life overhaul. Just a few thoughtful experiments you can open, adapt, or talk through with Buddy.',
      'banner': {
        'id': 'choose_one_kind_move',
        'title': 'Choose one kind move for future you',
        'category': 'Today',
        'summary':
            'Make one choice now that removes a little friction from tomorrow.',
        'why_it_fits':
            'Progress often feels better when it is a gift to your next self, not another demand on your current one.',
        'steps': [
          'Picture one annoying moment tomorrow.',
          'Do the smallest thing that makes it easier.',
          'Let that be enough for today.',
        ],
        'chat_prompt': 'Help me choose one kind move for future me.',
        'image_key': 'wellbeing',
        'personalized': false,
        'minutes': 5,
      },
      'ideas': selected,
      'next_cursor': page + 1,
      'generated_at': DateTime.now().toUtc().toIso8601String(),
    });
  }
}
