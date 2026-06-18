import 'package:flutter/foundation.dart';

/// One news item in the briefing: a short 2-3 line blurb plus an optional citation
/// into [DailyBriefing.sources] (the source that grounds it). The world snapshot
/// returns these; the scheduled digest has only a narrative and leaves [items] empty.
@immutable
class BriefingItem {
  final String text;

  /// Index into [DailyBriefing.sources] for this item's citation, or null when the
  /// item has no grounded source to cite.
  final int? citationIndex;

  /// Human category label for the on-screen chip (e.g. "Sports", "Tech & AI"). Empty
  /// for the world snapshot, which does not tag items by category.
  final String category;

  const BriefingItem({required this.text, this.citationIndex, this.category = ''});

  factory BriefingItem.fromJson(Map<String, dynamic> json) => BriefingItem(
        text: json['text'] as String? ?? '',
        citationIndex: (json['citation'] as num?)?.toInt(),
        category: json['category'] as String? ?? '',
      );
}

/// One source item the briefing narrative wove in. A small per-item citation links
/// to it so the user can tap through to what Buddy summarized.
@immutable
class BriefingSource {
  final String title;
  final String url;
  final String source;
  final String category;

  const BriefingSource({
    required this.title,
    required this.url,
    required this.source,
    required this.category,
  });

  factory BriefingSource.fromJson(Map<String, dynamic> json) => BriefingSource(
        title: json['title'] as String? ?? '',
        url: json['url'] as String? ?? '',
        source: json['source'] as String? ?? '',
        category: json['category'] as String? ?? '',
      );
}

/// The signed-in user's synthesized briefing for a single day, as returned by
/// `GET /briefing/today`. The backend (BriefingAgent) generated this from the
/// ranked content pool; the screen renders [narrative] and seeds the "Chat about
/// this" chat with [chatSeedMessage].
@immutable
class DailyBriefing {
  final String date; // user-local "YYYY-MM-DD"
  final String narrative;
  final String chatSeedMessage;
  final List<BriefingSource> sources;

  /// Discrete news items (world snapshot). Empty for the scheduled digest, which the
  /// screen then renders by splitting [narrative] into paragraphs.
  final List<BriefingItem> items;

  const DailyBriefing({
    required this.date,
    required this.narrative,
    required this.chatSeedMessage,
    required this.sources,
    this.items = const [],
  });

  factory DailyBriefing.fromJson(Map<String, dynamic> json) => DailyBriefing(
        date: json['date'] as String? ?? '',
        narrative: json['narrative'] as String? ?? '',
        chatSeedMessage: json['chat_seed_message'] as String? ?? '',
        sources: (json['sources'] as List?)
                ?.whereType<Map<String, dynamic>>()
                .map(BriefingSource.fromJson)
                .toList() ??
            const [],
        items: (json['items'] as List?)
                ?.whereType<Map<String, dynamic>>()
                .map(BriefingItem.fromJson)
                .toList() ??
            const [],
      );
}
