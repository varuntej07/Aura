enum MemoryCategory {
  preferences,
  facts,
  habits,
  health,
  routines,
  lifestyle,
  interests,
}

class MemoryModel {
  final String id;
  final String key;
  final String value;
  final MemoryCategory category;
  final String source; // voice, text, nutrition_scan, system
  final DateTime createdAt;
  final DateTime updatedAt;

  const MemoryModel({
    required this.id,
    required this.key,
    required this.value,
    required this.category,
    required this.source,
    required this.createdAt,
    required this.updatedAt,
  });

  factory MemoryModel.fromJson(Map<String, dynamic> json) {
    return MemoryModel(
      id: json['id'] as String,
      key: json['key'] as String,
      value: json['value'] as String,
      // NEVER byName() here. It throws on an unknown name, and getMemories maps
      // every doc in the collection through this factory inside one try, so a
      // single memory written with a category this build has not heard of fails
      // the ENTIRE list read. Prod accounts already hold categories the chat
      // surface wrote without validating ('project', 'bug report',
      // 'feature_request'), so this was silently emptying the memories screen.
      // Unknown categories degrade to facts and stay readable.
      category: MemoryCategory.values.asNameMap()[json['category'] as String?] ??
          MemoryCategory.facts,
      source: json['source'] as String? ?? 'system',
      createdAt: DateTime.parse(json['created_at'] as String),
      updatedAt: DateTime.parse(json['updated_at'] as String),
    );
  }

  Map<String, dynamic> toJson() => {
        'id': id,
        'key': key,
        'value': value,
        'category': category.name,
        'source': source,
        'created_at': createdAt.toUtc().toIso8601String(),
        'updated_at': updatedAt.toUtc().toIso8601String(),
      };
}
