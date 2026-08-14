enum SubscriptionTier { free, companion, pro }

enum SubscriptionStatus { trialing, active, expired, gracePeriod }

/// Trial length granted to every account, server-stamped by the backend on the
/// first GET /entitlement. Kept here only for paywall copy; the authoritative
/// clock is the backend's `trial_end_date`.
const int kTrialDurationDays = 45;

/// One metered daily allowance, as the backend counts it.
///
/// The counter is authoritative server-side and resets each UTC calendar day;
/// this is a read-only view for showing the user where they stand BEFORE they
/// hit the wall. Every value is clamped, so a counter that has run past its
/// limit (possible when a limit is lowered) still renders sanely.
class UsageCounter {
  final int used;
  final int limit;

  const UsageCounter({required this.used, required this.limit});

  int get remaining => (limit - used).clamp(0, limit);

  /// 0.0 to 1.0. Returns 1.0 for a non-positive limit so an unusable
  /// allowance never reads as "plenty left".
  double get fraction =>
      limit <= 0 ? 1.0 : (used / limit).clamp(0.0, 1.0).toDouble();

  bool get isExhausted => used >= limit;

  /// True once the user is close enough that silence would be a disservice.
  bool get isRunningLow => fraction >= 0.8;

  static UsageCounter? fromJson(dynamic json) {
    // The backend sends null for a counter whose read failed. Null means
    // "unknown", which must render as nothing at all rather than as zero used.
    if (json is! Map<String, dynamic>) return null;
    final used = (json['used'] as num?)?.toInt();
    final limit = (json['limit'] as num?)?.toInt();
    if (used == null || limit == null) return null;
    return UsageCounter(used: used, limit: limit);
  }
}

/// The daily usage block from GET /entitlement.
///
/// The backend has always served this; the app ignored it entirely, which is
/// why users hit the 25-message cap with no warning and no way to see it
/// coming. Deliberately NOT cached offline: a stale counter is worse than no
/// counter, because it invites the user to trust a number that has moved.
class UsageSummary {
  /// UTC day these counters belong to, e.g. `2026-08-12`.
  final String? date;
  final UsageCounter? chat;
  final UsageCounter? webSurf;
  final UsageCounter? drafts;
  final UsageCounter? voiceSeconds;

  const UsageSummary({
    this.date,
    this.chat,
    this.webSurf,
    this.drafts,
    this.voiceSeconds,
  });

  static UsageSummary? fromJson(dynamic json) {
    if (json is! Map<String, dynamic>) return null;
    return UsageSummary(
      date: json['date'] as String?,
      chat: UsageCounter.fromJson(json['chat']),
      webSurf: UsageCounter.fromJson(json['web_surf']),
      drafts: UsageCounter.fromJson(json['drafts']),
      voiceSeconds: UsageCounter.fromJson(json['voice_seconds']),
    );
  }
}

class UserEntitlement {
  final SubscriptionTier tier;
  final SubscriptionStatus status;
  final DateTime? expiresAt;
  final DateTime? trialEndDate;
  final bool cancelAtPeriodEnd;

  /// Whether the backend can actually create a checkout session right now.
  ///
  /// Served by GET /entitlement from `settings.dodo_configured`. Defaults to
  /// FALSE when absent, which matters for backward compatibility: an app build
  /// talking to an older backend that does not send the field must not render
  /// an Upgrade button. Showing no purchase path is recoverable; showing one
  /// that dead-ends is not.
  final bool checkoutAvailable;

  /// Today's metered usage. Null when unknown (served from cache, or offline).
  final UsageSummary? usage;

  /// The backend's authoritative access resolution (trial window counts as
  /// pro, expired resolves free). Preferred over any client-side math.
  final SubscriptionTier serverEffectiveTier;

  const UserEntitlement({
    required this.tier,
    required this.status,
    required this.serverEffectiveTier,
    this.expiresAt,
    this.trialEndDate,
    this.cancelAtPeriodEnd = false,
    this.checkoutAvailable = false,
    this.usage,
  });

  // Computed
  bool get isTrialActive =>
      status == SubscriptionStatus.trialing &&
      trialEndDate != null &&
      DateTime.now().isBefore(trialEndDate!);

  bool get isPaid => tier != SubscriptionTier.free;

  /// Checkout becomes available only after the free trial has ended. Paid
  /// accounts manage their existing subscription instead of buying again.
  bool get canPurchaseSubscription => !isTrialActive && !isPaid;

  /// Returns 0 when the trial has expired or the user is on a paid plan.
  int get daysLeftInTrial {
    if (!isTrialActive) return 0;
    return trialEndDate!
        .difference(DateTime.now())
        .inDays
        .clamp(0, kTrialDurationDays);
  }

  /// The tier the user actually gets access to. The backend already resolved
  /// the trial window and expiry, so this is a straight read.
  SubscriptionTier get effectiveTier => serverEffectiveTier;

  bool get hasFeatureAccess => effectiveTier != SubscriptionTier.free;

  /// Parses the GET /entitlement response (ISO 8601 timestamps, snake_case).
  factory UserEntitlement.fromBackend(Map<String, dynamic> json) {
    return UserEntitlement(
      tier: _parseTier(json['tier'] as String?),
      status: _parseStatus(json['status'] as String?),
      serverEffectiveTier: _parseTier(json['effective_tier'] as String?),
      expiresAt: _parseDate(json['expires_at']),
      trialEndDate: _parseDate(json['trial_end_date']),
      cancelAtPeriodEnd: json['cancel_at_period_end'] == true,
      checkoutAvailable: json['checkout_available'] == true,
      usage: UsageSummary.fromJson(json['usage']),
    );
  }

  /// Round-trips through the local offline cache (SharedPreferences JSON).
  factory UserEntitlement.fromCacheJson(Map<String, dynamic> json) =>
      UserEntitlement.fromBackend(json);

  Map<String, dynamic> toCacheJson() => {
    'tier': tier.name,
    'status': status.name,
    'effective_tier': serverEffectiveTier.name,
    if (expiresAt != null) 'expires_at': expiresAt!.toIso8601String(),
    if (trialEndDate != null) 'trial_end_date': trialEndDate!.toIso8601String(),
    'cancel_at_period_end': cancelAtPeriodEnd,
    // Cached: it is a backend config fact, stable across a session.
    'checkout_available': checkoutAvailable,
    // `usage` is deliberately NOT cached. It is a per-UTC-day counter, so a
    // cached copy is wrong the moment the day rolls over or the user sends a
    // message on another device. Absent from the cache means fromBackend parses
    // it as null, which the UI renders as "unknown" rather than as a stale
    // number the user would reasonably act on.
  };

  // Private parsers
  static SubscriptionTier _parseTier(String? value) => SubscriptionTier.values
      .firstWhere((t) => t.name == value, orElse: () => SubscriptionTier.free);

  static SubscriptionStatus _parseStatus(String? value) {
    // Backend values (trialing, active, gracePeriod, expired) match the enum
    // names exactly. Anything unrecognized resolves expired: never grant
    // access off a value this client doesn't understand.
    return SubscriptionStatus.values.firstWhere(
      (s) => s.name == value,
      orElse: () => SubscriptionStatus.expired,
    );
  }

  static DateTime? _parseDate(dynamic value) {
    if (value is! String || value.isEmpty) return null;
    return DateTime.tryParse(value);
  }
}
