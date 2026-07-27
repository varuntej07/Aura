class GetBetterIdea {
  final String id;
  final String title;
  final String category;
  final String summary;
  final String whyItFits;
  final List<String> steps;
  final String chatPrompt;
  final String imageKey;
  final bool personalized;
  final int minutes;
  final int storyVersion;
  final String narrative;
  final String whatItMeans;
  final String tryThis;
  final List<String> relatedStoryIds;
  final String cardType;
  final int displayOrder;
  final bool featured;

  const GetBetterIdea({
    required this.id,
    required this.title,
    required this.category,
    required this.summary,
    required this.whyItFits,
    required this.steps,
    required this.chatPrompt,
    required this.imageKey,
    required this.personalized,
    required this.minutes,
    required this.storyVersion,
    required this.narrative,
    required this.whatItMeans,
    required this.tryThis,
    required this.relatedStoryIds,
    required this.cardType,
    required this.displayOrder,
    required this.featured,
  });

  factory GetBetterIdea.fromJson(Map<String, dynamic> json) {
    final summary = json['summary'] as String? ?? '';
    final whyItFits = json['why_it_fits'] as String? ?? '';
    final steps =
        (json['steps'] as List<dynamic>?)
            ?.whereType<String>()
            .where((step) => step.trim().isNotEmpty)
            .toList(growable: false) ??
        const <String>[];
    return GetBetterIdea(
      id: json['id'] as String? ?? '',
      title: json['title'] as String? ?? '',
      category: json['category'] as String? ?? '',
      summary: summary,
      whyItFits: whyItFits,
      steps: steps,
      chatPrompt: json['chat_prompt'] as String? ?? '',
      imageKey: json['image_key'] as String? ?? 'momentum',
      personalized: json['personalized'] as bool? ?? false,
      minutes: (json['minutes'] as num?)?.toInt() ?? 10,
      storyVersion: (json['story_version'] as num?)?.toInt() ?? 1,
      narrative: json['narrative'] as String? ?? summary,
      whatItMeans: json['what_it_means'] as String? ?? whyItFits,
      tryThis:
          json['try_this'] as String? ??
          (steps.isNotEmpty ? steps.first : summary),
      relatedStoryIds:
          (json['related_story_ids'] as List<dynamic>?)
              ?.whereType<String>()
              .where((storyId) => storyId.trim().isNotEmpty)
              .toList(growable: false) ??
          const <String>[],
      cardType: json['card_type'] as String? ?? 'square',
      displayOrder: (json['display_order'] as num?)?.toInt() ?? 0,
      featured: json['featured'] as bool? ?? false,
    );
  }

  Map<String, dynamic> toJson() => {
    'id': id,
    'title': title,
    'category': category,
    'summary': summary,
    'why_it_fits': whyItFits,
    'steps': steps,
    'chat_prompt': chatPrompt,
    'image_key': imageKey,
    'personalized': personalized,
    'minutes': minutes,
    'story_version': storyVersion,
    'narrative': narrative,
    'what_it_means': whatItMeans,
    'try_this': tryThis,
    'related_story_ids': relatedStoryIds,
    'card_type': cardType,
    'display_order': displayOrder,
    'featured': featured,
  };
}

class GetBetterFeed {
  final String headline;
  final String intro;
  final GetBetterIdea banner;
  final List<GetBetterIdea> ideas;
  final int nextCursor;
  final DateTime generatedAt;
  final String catalogVersion;

  const GetBetterFeed({
    required this.headline,
    required this.intro,
    required this.banner,
    required this.ideas,
    required this.nextCursor,
    required this.generatedAt,
    required this.catalogVersion,
  });

  factory GetBetterFeed.fromJson(Map<String, dynamic> json) {
    return GetBetterFeed(
      headline: json['headline'] as String? ?? 'A little better, your way',
      intro:
          json['intro'] as String? ??
          'Small stories that can help without turning life into a checklist.',
      banner: GetBetterIdea.fromJson(
        json['banner'] as Map<String, dynamic>? ?? const {},
      ),
      ideas:
          (json['ideas'] as List<dynamic>?)
              ?.whereType<Map<String, dynamic>>()
              .map(GetBetterIdea.fromJson)
              .where((idea) => idea.id.isNotEmpty && idea.title.isNotEmpty)
              .toList(growable: false) ??
          const [],
      nextCursor: (json['next_cursor'] as num?)?.toInt() ?? 0,
      generatedAt:
          DateTime.tryParse(json['generated_at'] as String? ?? '') ??
          DateTime.now(),
      catalogVersion: json['catalog_version'] as String? ?? 'legacy',
    );
  }

  Map<String, dynamic> toJson() => {
    'headline': headline,
    'intro': intro,
    'banner': banner.toJson(),
    'ideas': ideas.map((idea) => idea.toJson()).toList(growable: false),
    'next_cursor': nextCursor,
    'generated_at': generatedAt.toUtc().toIso8601String(),
    'catalog_version': catalogVersion,
  };

  List<GetBetterIdea> get allStories => [banner, ...ideas];

  GetBetterIdea? storyById(String storyId) {
    for (final story in allStories) {
      if (story.id == storyId) return story;
    }
    return null;
  }
}

class GetBetterCatalogSync {
  final bool notModified;
  final String catalogVersion;
  final GetBetterFeed? feed;

  const GetBetterCatalogSync({
    required this.notModified,
    required this.catalogVersion,
    required this.feed,
  });

  factory GetBetterCatalogSync.fromJson(Map<String, dynamic> json) {
    final rawFeed = json['feed'];
    return GetBetterCatalogSync(
      notModified: json['not_modified'] as bool? ?? false,
      catalogVersion: json['catalog_version'] as String? ?? '',
      feed: rawFeed is Map<String, dynamic>
          ? GetBetterFeed.fromJson(rawFeed)
          : null,
    );
  }
}

class GetBetterActivityEvent {
  final String eventId;
  final String eventType;
  final String storyId;
  final int storyVersion;
  final DateTime occurredAt;

  const GetBetterActivityEvent({
    required this.eventId,
    required this.eventType,
    required this.storyId,
    required this.storyVersion,
    required this.occurredAt,
  });

  Map<String, dynamic> toJson() => {
    'event_id': eventId,
    'event_type': eventType,
    'story_id': storyId,
    'story_version': storyVersion,
    'occurred_at': occurredAt.toUtc().toIso8601String(),
  };
}
