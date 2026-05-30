import 'package:cloud_firestore/cloud_firestore.dart';

enum SubscriptionTier { free, companion, pro }

enum SubscriptionStatus { active, expired, gracePeriod }

/// How the entitlement was granted. Kept in Firestore so the backend
/// entitlement middleware can handle each platform's receipt format correctly,
/// and so web/promo grants can be added later without schema changes.
enum EntitlementPlatform { ios, android, promo, web }

/// Trial length granted to every new account. 7 days is the beta-launch default
/// — long enough to form a daily-use habit on voice, short enough that users
/// don't forget they signed up.
const int kTrialDurationDays = 7;

class UserEntitlement {
  final SubscriptionTier tier;
  final SubscriptionStatus status;
  final EntitlementPlatform? platform;
  final String? productId;
  final DateTime? expiresAt;
  final DateTime trialStartDate;
  final DateTime trialEndDate;
  final DateTime? originalPurchaseDate;
  final DateTime updatedAt;

  const UserEntitlement({
    required this.tier,
    required this.status,
    required this.trialStartDate,
    required this.trialEndDate,
    required this.updatedAt,
    this.platform,
    this.productId,
    this.expiresAt,
    this.originalPurchaseDate,
  });

  // Computed
  bool get isTrialActive =>
      tier == SubscriptionTier.free &&
      DateTime.now().isBefore(trialEndDate);

  bool get isPaid => tier != SubscriptionTier.free;

  /// Returns 0 when trial has expired or user is on a paid plan.
  int get daysLeftInTrial {
    if (!isTrialActive) return 0;
    return trialEndDate.difference(DateTime.now()).inDays.clamp(0, kTrialDurationDays);
  }

  /// The effective tier the user should receive access to.
  /// During trial, the user gets Pro access regardless of their tier field.
  SubscriptionTier get effectiveTier =>
      isTrialActive ? SubscriptionTier.pro : tier;

  bool get hasFeatureAccess => effectiveTier != SubscriptionTier.free;

  // Factory
  factory UserEntitlement.fromFirestore(Map<String, dynamic> data) {
    final now = DateTime.now();
    final trialStart = (data['trial_start_date'] as Timestamp?)?.toDate() ?? now;
    final trialEnd = (data['trial_end_date'] as Timestamp?)?.toDate() ??
        trialStart.add(const Duration(days: kTrialDurationDays));

    return UserEntitlement(
      tier: _parseTier(data['tier'] as String?),
      status: _parseStatus(data['status'] as String?),
      platform: _parsePlatform(data['platform'] as String?),
      productId: data['product_id'] as String?,
      expiresAt: (data['expires_at'] as Timestamp?)?.toDate(),
      trialStartDate: trialStart,
      trialEndDate: trialEnd,
      originalPurchaseDate: (data['original_purchase_date'] as Timestamp?)?.toDate(),
      updatedAt: (data['updated_at'] as Timestamp?)?.toDate() ?? now,
    );
  }

  /// Default entitlement for a brand-new user. Trial starts immediately.
  factory UserEntitlement.newUser() {
    final now = DateTime.now();
    return UserEntitlement(
      tier: SubscriptionTier.free,
      status: SubscriptionStatus.active,
      trialStartDate: now,
      trialEndDate: now.add(const Duration(days: kTrialDurationDays)),
      updatedAt: now,
    );
  }

  Map<String, dynamic> toFirestore() => {
        'tier': tier.name,
        'status': status.name,
        if (platform != null) 'platform': platform!.name,
        if (productId != null) 'product_id': productId,
        if (expiresAt != null) 'expires_at': Timestamp.fromDate(expiresAt!),
        'trial_start_date': Timestamp.fromDate(trialStartDate),
        'trial_end_date': Timestamp.fromDate(trialEndDate),
        if (originalPurchaseDate != null)
          'original_purchase_date': Timestamp.fromDate(originalPurchaseDate!),
        'updated_at': FieldValue.serverTimestamp(),
      };

  UserEntitlement copyWith({
    SubscriptionTier? tier,
    SubscriptionStatus? status,
    EntitlementPlatform? platform,
    String? productId,
    DateTime? expiresAt,
    DateTime? trialStartDate,
    DateTime? trialEndDate,
    DateTime? originalPurchaseDate,
    DateTime? updatedAt,
  }) =>
      UserEntitlement(
        tier: tier ?? this.tier,
        status: status ?? this.status,
        platform: platform ?? this.platform,
        productId: productId ?? this.productId,
        expiresAt: expiresAt ?? this.expiresAt,
        trialStartDate: trialStartDate ?? this.trialStartDate,
        trialEndDate: trialEndDate ?? this.trialEndDate,
        originalPurchaseDate: originalPurchaseDate ?? this.originalPurchaseDate,
        updatedAt: updatedAt ?? this.updatedAt,
      );

  // Private parsers
  static SubscriptionTier _parseTier(String? value) =>
      SubscriptionTier.values.firstWhere(
        (t) => t.name == value,
        orElse: () => SubscriptionTier.free,
      );

  static SubscriptionStatus _parseStatus(String? value) =>
      SubscriptionStatus.values.firstWhere(
        (s) => s.name == value,
        orElse: () => SubscriptionStatus.active,
      );

  static EntitlementPlatform? _parsePlatform(String? value) {
    if (value == null) return null;
    return EntitlementPlatform.values.firstWhere(
      (p) => p.name == value,
      orElse: () => EntitlementPlatform.ios,
    );
  }
}

/// Product IDs as defined in App Store Connect and Play Console.
/// Single source of truth — never hardcode these at callsites.
class SubscriptionProductIds {
  SubscriptionProductIds._();

  static const String companionMonthly = 'aura_companion_monthly';
  static const String companionAnnual = 'aura_companion_annual';
  static const String proMonthly = 'aura_pro_monthly';
  static const String proAnnual = 'aura_pro_annual';

  static const Set<String> all = {
    companionMonthly,
    companionAnnual,
    proMonthly,
    proAnnual,
  };

  static SubscriptionTier tierForProductId(String productId) {
    if (productId == companionMonthly || productId == companionAnnual) {
      return SubscriptionTier.companion;
    }
    if (productId == proMonthly || productId == proAnnual) {
      return SubscriptionTier.pro;
    }
    return SubscriptionTier.free;
  }
}
