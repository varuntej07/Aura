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
  });

  factory GetBetterIdea.fromJson(Map<String, dynamic> json) {
    return GetBetterIdea(
      id: json['id'] as String? ?? '',
      title: json['title'] as String? ?? '',
      category: json['category'] as String? ?? '',
      summary: json['summary'] as String? ?? '',
      whyItFits: json['why_it_fits'] as String? ?? '',
      steps:
          (json['steps'] as List<dynamic>?)
              ?.whereType<String>()
              .where((step) => step.trim().isNotEmpty)
              .toList(growable: false) ??
          const [],
      chatPrompt: json['chat_prompt'] as String? ?? '',
      imageKey: json['image_key'] as String? ?? 'momentum',
      personalized: json['personalized'] as bool? ?? false,
      minutes: (json['minutes'] as num?)?.toInt() ?? 10,
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
  };
}

class GetBetterFeed {
  final String headline;
  final String intro;
  final GetBetterIdea banner;
  final List<GetBetterIdea> ideas;
  final int nextCursor;
  final DateTime generatedAt;

  const GetBetterFeed({
    required this.headline,
    required this.intro,
    required this.banner,
    required this.ideas,
    required this.nextCursor,
    required this.generatedAt,
  });

  factory GetBetterFeed.fromJson(Map<String, dynamic> json) {
    return GetBetterFeed(
      headline: json['headline'] as String? ?? 'A little better, your way',
      intro:
          json['intro'] as String? ??
          'Small experiments, picked to help without turning your life into a checklist.',
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
      nextCursor: (json['next_cursor'] as num?)?.toInt() ?? 1,
      generatedAt:
          DateTime.tryParse(json['generated_at'] as String? ?? '') ??
          DateTime.now(),
    );
  }

  Map<String, dynamic> toJson() => {
    'headline': headline,
    'intro': intro,
    'banner': banner.toJson(),
    'ideas': ideas.map((idea) => idea.toJson()).toList(growable: false),
    'next_cursor': nextCursor,
    'generated_at': generatedAt.toUtc().toIso8601String(),
  };

  GetBetterFeed copyWith({
    String? headline,
    String? intro,
    GetBetterIdea? banner,
    List<GetBetterIdea>? ideas,
    int? nextCursor,
    DateTime? generatedAt,
  }) {
    return GetBetterFeed(
      headline: headline ?? this.headline,
      intro: intro ?? this.intro,
      banner: banner ?? this.banner,
      ideas: ideas ?? this.ideas,
      nextCursor: nextCursor ?? this.nextCursor,
      generatedAt: generatedAt ?? this.generatedAt,
    );
  }
}
