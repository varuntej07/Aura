import 'package:aura/core/network/api_client.dart';
import 'package:aura/data/models/subscription_plan.dart';
import 'package:aura/data/services/firebase_auth_service.dart';
import 'package:aura/data/services/posthog_analytics_service.dart';
import 'package:aura/data/services/subscription_service.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:mocktail/mocktail.dart';
import 'package:shared_preferences/shared_preferences.dart';

class MockApiClient extends Mock implements ApiClient {}

class MockFirebaseAuthService extends Mock implements FirebaseAuthService {}

class MockPostHogAnalyticsService extends Mock implements PostHogAnalyticsService {}

const _allLinkOut = SteeringConfig(
  androidUs: SteeringMode.linkOut,
  iosUs: SteeringMode.linkOut,
  restOfWorld: SteeringMode.linkOut,
);

const _rowSilent = SteeringConfig(
  androidUs: SteeringMode.linkOut,
  iosUs: SteeringMode.linkOut,
  restOfWorld: SteeringMode.silent,
);

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

  group('steeringMode', () {
    test('unknown country is always SILENT, even when steering allows link-out', () {
      // The backend could not resolve a country: the paywall must fall to the
      // always-legal mode regardless of what the steering config would allow.
      sut.debugSetSteeringState(_allLinkOut, null);
      expect(sut.steeringMode, SteeringMode.silent);

      sut.debugSetSteeringState(_allLinkOut, '');
      expect(sut.steeringMode, SteeringMode.silent);
    });

    test('non-US country gets the rest-of-world mode', () {
      sut.debugSetSteeringState(_rowSilent, 'DE');
      expect(sut.steeringMode, SteeringMode.silent);

      sut.debugSetSteeringState(_allLinkOut, 'IN');
      expect(sut.steeringMode, SteeringMode.linkOut);
    });

    test('US on a non-store platform stays conservative (rest-of-world)', () {
      // Test hosts are desktop, neither Android nor iOS: the US branch must
      // fall through to the rest-of-world mode, never assume a storefront.
      sut.debugSetSteeringState(_rowSilent, 'US');
      expect(sut.steeringMode, SteeringMode.silent);
    });
  });

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
