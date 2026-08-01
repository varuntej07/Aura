enum ReminderStatus { pending, fired, dismissed, snoozed }

/// Centralised status classification — update here when new statuses are added.
extension ReminderStatusX on ReminderStatus {
  /// Visible in the "Upcoming" section. Includes [fired] because the
  /// notification being delivered does NOT mean the user has acknowledged it.
  bool get isActive =>
      this == ReminderStatus.pending ||
      this == ReminderStatus.snoozed ||
      this == ReminderStatus.fired;

  /// Visible in the "Completed" section — only explicit user dismissal qualifies.
  bool get isCompleted => this == ReminderStatus.dismissed;
}

class ReminderModel {
  final String id;
  final String message;
  final DateTime triggerAt;
  final ReminderStatus status;
  final String createdVia; // voice, text, notification_reply
  final int snoozeCount;
  final DateTime createdAt;
  final DateTime? firedAt;
  final DateTime? dismissedAt;

  const ReminderModel({
    required this.id,
    required this.message,
    required this.triggerAt,
    required this.status,
    required this.createdVia,
    required this.snoozeCount,
    required this.createdAt,
    this.firedAt,
    this.dismissedAt,
  });

  // Sentinel used by copyWith to distinguish "not provided" from explicit null.
  static const Object _absent = Object();

  /// Creates a copy with overridden fields.
  ///
  /// Nullable fields ([firedAt], [dismissedAt]) use an internal sentinel so
  /// you can explicitly clear them by passing `null`:
  /// ```dart
  /// reminder.copyWith(dismissedAt: null) // clears the field
  /// reminder.copyWith()                  // keeps the existing value
  /// ```
  ReminderModel copyWith({
    String? id,
    String? message,
    DateTime? triggerAt,
    ReminderStatus? status,
    String? createdVia,
    int? snoozeCount,
    DateTime? createdAt,
    Object? firedAt = _absent,
    Object? dismissedAt = _absent,
  }) {
    return ReminderModel(
      id: id ?? this.id,
      message: message ?? this.message,
      triggerAt: triggerAt ?? this.triggerAt,
      status: status ?? this.status,
      createdVia: createdVia ?? this.createdVia,
      snoozeCount: snoozeCount ?? this.snoozeCount,
      createdAt: createdAt ?? this.createdAt,
      firedAt: firedAt == _absent ? this.firedAt : firedAt as DateTime?,
      dismissedAt:
          dismissedAt == _absent ? this.dismissedAt : dismissedAt as DateTime?,
    );
  }

  factory ReminderModel.fromJson(Map<String, dynamic> json) {
    return ReminderModel(
      id: json['id'] as String,
      message: json['message'] as String,
      triggerAt: DateTime.parse(json['trigger_at'] as String),
      status: ReminderStatus.values.byName(
        json['status'] as String? ?? 'pending',
      ),
      createdVia: json['created_via'] as String? ?? 'text',
      snoozeCount: json['snooze_count'] as int? ?? 0,
      createdAt: DateTime.parse(json['created_at'] as String),
      firedAt: json['fired_at'] != null
          ? DateTime.parse(json['fired_at'] as String)
          : null,
      dismissedAt: json['dismissed_at'] != null
          ? DateTime.parse(json['dismissed_at'] as String)
          : null,
    );
  }

  Map<String, dynamic> toJson() => {
        'id': id,
        'message': message,
        'trigger_at': triggerAt.toUtc().toIso8601String(),
        'status': status.name,
        'created_via': createdVia,
        'snooze_count': snoozeCount,
        'created_at': createdAt.toUtc().toIso8601String(),
        'fired_at': firedAt?.toUtc().toIso8601String(),
        'dismissed_at': dismissedAt?.toUtc().toIso8601String(),
      };
}
