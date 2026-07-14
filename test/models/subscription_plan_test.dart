import 'package:aura/data/models/subscription_plan.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  group('UserEntitlement.canPurchaseSubscription', () {
    test('is false throughout the active 45-day trial', () {
      final entitlement = UserEntitlement(
        tier: SubscriptionTier.free,
        status: SubscriptionStatus.trialing,
        serverEffectiveTier: SubscriptionTier.pro,
        trialEndDate: DateTime.now().add(const Duration(days: 45)),
      );

      expect(entitlement.isTrialActive, isTrue);
      expect(entitlement.canPurchaseSubscription, isFalse);
    });

    test('is true after the free trial expires', () {
      final entitlement = UserEntitlement(
        tier: SubscriptionTier.free,
        status: SubscriptionStatus.expired,
        serverEffectiveTier: SubscriptionTier.free,
        trialEndDate: DateTime.now().subtract(const Duration(seconds: 1)),
      );

      expect(entitlement.canPurchaseSubscription, isTrue);
    });

    test('is false for an existing paid subscription', () {
      const entitlement = UserEntitlement(
        tier: SubscriptionTier.companion,
        status: SubscriptionStatus.active,
        serverEffectiveTier: SubscriptionTier.companion,
      );

      expect(entitlement.canPurchaseSubscription, isFalse);
    });
  });
}
