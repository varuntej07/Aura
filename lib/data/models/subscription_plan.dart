enum SubscriptionTier { free, companion, pro }

enum SubscriptionStatus { trialing, active, expired, gracePeriod }

/// What the paywall is allowed to do on this storefront, served by the backend
/// inside GET /entitlement so store-policy reactions are backend config flips,
/// never app releases.
///
/// [linkOut] = the paywall may link out to web checkout (US storefronts).
/// [silent] = plan status only, no purchase mention at all (the always-legal
/// Netflix model, everywhere else).
enum SteeringMode { linkOut, silent }

/// Trial length granted to every account, server-stamped by the backend on the
/// first GET /entitlement. Kept here only for paywall copy; the authoritative
/// clock is the backend's `trial_end_date`.
const int kTrialDurationDays = 45;

class UserEntitlement {
  final SubscriptionTier tier;
  final SubscriptionStatus status;
  final DateTime? expiresAt;
  final DateTime? trialEndDate;
  final bool cancelAtPeriodEnd;

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

/// The steering block from GET /entitlement: which paywall behavior each
/// storefront gets. Defaults to silent everywhere, the always-legal mode, so
/// a missing or malformed block can never show a purchase UI it shouldn't.
class SteeringConfig {
  final SteeringMode androidUs;
  final SteeringMode iosUs;
  final SteeringMode restOfWorld;

  const SteeringConfig({
    required this.androidUs,
    required this.iosUs,
    required this.restOfWorld,
  });

  static const SteeringConfig allSilent = SteeringConfig(
    androidUs: SteeringMode.silent,
    iosUs: SteeringMode.silent,
    restOfWorld: SteeringMode.silent,
  );

  factory SteeringConfig.fromBackend(Map<String, dynamic>? json) {
    if (json == null) return allSilent;
    return SteeringConfig(
      androidUs: _parseMode(json['android_us']),
      iosUs: _parseMode(json['ios_us']),
      restOfWorld: _parseMode(json['row']),
    );
  }

  Map<String, dynamic> toCacheJson() => {
    'android_us': androidUs == SteeringMode.linkOut ? 'LINK_OUT' : 'SILENT',
    'ios_us': iosUs == SteeringMode.linkOut ? 'LINK_OUT' : 'SILENT',
    'row': restOfWorld == SteeringMode.linkOut ? 'LINK_OUT' : 'SILENT',
  };

  static SteeringMode _parseMode(dynamic value) =>
      value == 'LINK_OUT' ? SteeringMode.linkOut : SteeringMode.silent;
}
