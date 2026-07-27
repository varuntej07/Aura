import 'package:flutter_test/flutter_test.dart';

import 'package:aura/data/models/get_better_feed.dart';

void main() {
  test('story JSON round trip preserves narrative and relationship fields', () {
    final original = _story();

    final decoded = GetBetterIdea.fromJson(original.toJson());

    expect(decoded.id, original.id);
    expect(decoded.storyVersion, original.storyVersion);
    expect(decoded.narrative, original.narrative);
    expect(decoded.whatItMeans, original.whatItMeans);
    expect(decoded.tryThis, original.tryThis);
    expect(decoded.relatedStoryIds, original.relatedStoryIds);
    expect(decoded.cardType, original.cardType);
  });

  test('legacy story payload receives safe narrative fallbacks', () {
    final decoded = GetBetterIdea.fromJson({
      'id': 'legacy',
      'title': 'A legacy story',
      'summary': 'A short summary.',
      'why_it_fits': 'A clear reason.',
      'steps': ['Try one thing.'],
    });

    expect(decoded.narrative, 'A short summary.');
    expect(decoded.whatItMeans, 'A clear reason.');
    expect(decoded.tryThis, 'Try one thing.');
    expect(decoded.storyVersion, 1);
    expect(decoded.cardType, 'square');
  });
}

GetBetterIdea _story() {
  return const GetBetterIdea(
    id: 'story-one',
    title: 'Take one small step',
    category: 'Focus',
    summary: 'A small step can make a hard job feel possible.',
    whyItFits: 'Starting is often the hardest part.',
    steps: ['Choose one tiny action.'],
    chatPrompt: 'Help me choose one small step.',
    imageKey: 'focus',
    personalized: false,
    minutes: 5,
    storyVersion: 2,
    narrative: 'Sam had a big job. He chose one small piece and began there.',
    whatItMeans: 'You do not need to finish everything at once.',
    tryThis: 'Pick one action that takes less than five minutes.',
    relatedStoryIds: ['story-two'],
    cardType: 'wide',
    displayOrder: 1,
    featured: false,
  );
}
