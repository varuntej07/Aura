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

/// How loud a reminder is. An alarm rings at alarm volume, turns the screen on,
/// and pierces Do Not Disturb; a reminder is a silent notification banner.
///
/// Buddy chooses this from the user's own words at creation time. There is no
/// settings toggle, and an absent field means [reminder]: every document written
/// before the alarm tier existed is a plain reminder.
enum ReminderTier { reminder, alarm }

class ReminderModel {
  final String id;
  final String message;
  final DateTime triggerAt;
  final ReminderStatus status;
  final ReminderTier tier;
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
    this.tier = ReminderTier.reminder,
    this.firedAt,
    this.dismissedAt,
  });

  bool get isAlarm => tier == ReminderTier.alarm;

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
    ReminderTier? tier,
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
      tier: tier ?? this.tier,
      createdVia: createdVia ?? this.createdVia,
      snoozeCount: snoozeCount ?? this.snoozeCount,
      createdAt: createdAt ?? this.createdAt,
      firedAt: firedAt == _absent ? this.firedAt : firedAt as DateTime?,
      dismissedAt:
          dismissedAt == _absent ? this.dismissedAt : dismissedAt as DateTime?,
    );
  }

  static ReminderStatus _statusFrom(String? raw) {
    for (final status in ReminderStatus.values) {
      if (status.name == raw) return status;
    }
    return ReminderStatus.pending;
  }

  factory ReminderModel.fromJson(Map<String, dynamic> json) {
    return ReminderModel(
      id: json['id'] as String,
      message: json['message'] as String,
      triggerAt: DateTime.parse(json['trigger_at'] as String),
      // Tolerant, not byName. The backend has a "processing" status (a reminder
      // the scheduler has claimed but not yet delivered) that this enum has
      // never modelled, and byName THROWS on it, taking the whole reminders
      // list down with it. An in-flight reminder is still pending from the
      // user's point of view, and so is anything else unrecognised.
      status: _statusFrom(json['status'] as String?),
      tier: (json['tier'] as String?) == 'alarm'
          ? ReminderTier.alarm
          : ReminderTier.reminder,
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
        'tier': tier.name,
        'created_via': createdVia,
        'snooze_count': snoozeCount,
        'created_at': createdAt.toUtc().toIso8601String(),
        'fired_at': firedAt?.toUtc().toIso8601String(),
        'dismissed_at': dismissedAt?.toUtc().toIso8601String(),
      };
}
