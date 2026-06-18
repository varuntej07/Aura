import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

// ConnectivityPlatform (the test seam) and ConnectivityResult both come from the
// platform interface; connectivity_plus only re-exports the latter.
// ignore: depend_on_referenced_packages
import 'package:connectivity_plus_platform_interface/connectivity_plus_platform_interface.dart';

import 'package:aura/core/errors/app_exception.dart';
import 'package:aura/core/network/api_client.dart';
import 'package:aura/core/network/connectivity_service.dart';

/// Forces [ConnectivityService] to report "connected" so the request actually
/// reaches the HTTP layer (otherwise `_execute` short-circuits with
/// networkUnavailable before we can exercise the ClientException path, and the
/// test would pass even without the fix).
class _AlwaysConnected extends ConnectivityPlatform {
  @override
  Future<List<ConnectivityResult>> checkConnectivity() async =>
      [ConnectivityResult.wifi];

  @override
  Stream<List<ConnectivityResult>> get onConnectivityChanged =>
      const Stream.empty();
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  setUp(() {
    ConnectivityPlatform.instance = _AlwaysConnected();
  });

  test(
      'a transport ClientException (e.g. connection abort when the app '
      'backgrounds) maps to a benign network failure, not an unexpected error',
      () async {
    final client = ApiClient(
      connectivity: ConnectivityService(),
      tokenProvider: ({bool forceRefresh = false}) async => 'test-token',
    );

    // Stands in for the OS aborting the socket mid-request (app backgrounded):
    // the http package surfaces it as a ClientException, which is none of the
    // typed SocketException / HttpException / TimeoutException the client
    // special-cases — so before the fix it fell into the generic catch.
    final mockClient = MockClient((request) async {
      throw http.ClientException(
        'Software caused connection abort',
        request.url,
      );
    });

    final result = await http.runWithClient(
      () => client.post(
        '/chat/buddy-pills/refresh',
        const <String, dynamic>{},
        (json) => json,
      ),
      () => mockClient,
    );

    // Without the fix this is ErrorCode.unexpected (logged ERROR + filed as a
    // Crashlytics non-fatal). With the fix it's a quiet network failure.
    expect(result.isFailure, isTrue);
    expect(result.errorOrNull?.code, ErrorCode.networkUnavailable);
  });
}
