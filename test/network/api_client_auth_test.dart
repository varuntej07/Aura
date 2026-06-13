import 'package:flutter_test/flutter_test.dart';
import 'package:aura/core/errors/app_exception.dart';
import 'package:aura/core/network/api_client.dart';
import 'package:aura/core/network/connectivity_service.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  group('ApiClient auth token handling', () {
    test(
        'streamPost retries the token with a forced refresh, then fails loud '
        'when no token can be obtained (never sends an unauthenticated request)',
        () async {
      // Simulates a signed-in user whose Firebase ID token cannot be fetched
      // (expired cache + a failed refresh on a cold launch from a notification
      // tap). Both the cached and forced-refresh calls return null.
      final forceRefreshFlags = <bool>[];
      final client = ApiClient(
        connectivity: ConnectivityService(),
        tokenProvider: ({bool forceRefresh = false}) async {
          forceRefreshFlags.add(forceRefresh);
          return null;
        },
      );

      // Without the fix the request would go out with no Authorization header
      // and the backend would reject it as "missing user_id". With the fix the
      // client throws a clear, retryable session error before any network call.
      await expectLater(
        client.streamPost('/chat', {'message': 'hi'}).toList(),
        throwsA(isA<AppException>()
            .having((e) => e.code, 'code', ErrorCode.authTokenExpired)),
      );

      // Proves the recovery attempt: a cached miss is retried once with force.
      expect(forceRefreshFlags, [false, true]);
    });
  });
}
