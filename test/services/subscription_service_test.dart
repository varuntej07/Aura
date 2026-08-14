import 'package:aura/core/network/api_client.dart';
import 'package:aura/data/services/firebase_auth_service.dart';
import 'package:aura/data/services/posthog_analytics_service.dart';
import 'package:aura/data/services/subscription_service.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:mocktail/mocktail.dart';
import 'package:shared_preferences/shared_preferences.dart';

class MockApiClient extends Mock implements ApiClient {}

class MockFirebaseAuthService extends Mock implements FirebaseAuthService {}

class MockPostHogAnalyticsService extends Mock implements PostHogAnalyticsService {}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  late SubscriptionService sut;

  setUp(() {
    sut = SubscriptionService(
      authService: MockFirebaseAuthService(),
      postHogAnalyticsService: MockPostHogAnalyticsService(),
      apiClient: MockApiClient(),
    );
  });

  // The `steeringMode` group that lived here verified per-country storefront
  // gating of the paywall. That mechanism is deleted: web checkout is offered in
  // every country, so there is no country branch left to assert on.

  group('consumePendingBackgroundRefresh', () {
    test('consumes the flag and refetches entitlement', () async {
      SharedPreferences.setMockInitialValues({
        kEntitlementRefreshPendingKey: true,
      });

      expect(sut.entitlement, isNull);
      await sut.consumePendingBackgroundRefresh();

      final prefs = await SharedPreferences.getInstance();
      expect(prefs.getBool(kEntitlementRefreshPendingKey), isNull);
      // Under flutter test the refetch is the dev bypass, which proves
      // refreshEntitlement actually ran.
      expect(sut.entitlement, isNotNull);
    });

    test('is a no-op when no background push arrived', () async {
      SharedPreferences.setMockInitialValues({});

      await sut.consumePendingBackgroundRefresh();

      expect(sut.entitlement, isNull);
    });
  });
}
