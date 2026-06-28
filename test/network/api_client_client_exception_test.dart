import 'dart:io';

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

/// Reports a fixed connectivity state to [ConnectivityService] so we can test
/// both an online device (a transit failure) and an offline one (a genuine
/// connectivity loss) and assert they produce DIFFERENT copy.
class _FixedConnectivity extends ConnectivityPlatform {
  _FixedConnectivity(this._online);
  final bool _online;

  @override
  Future<List<ConnectivityResult>> checkConnectivity() async =>
      _online ? [ConnectivityResult.wifi] : [ConnectivityResult.none];

  @override
  Stream<List<ConnectivityResult>> get onConnectivityChanged =>
      const Stream.empty();
}

ApiClient _client() => ApiClient(
      connectivity: ConnectivityService(),
      tokenProvider: ({bool forceRefresh = false}) async => 'test-token',
    );

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  test(
      'an online transport ClientException (connection abort) is a benign '
      'server-side failure, NOT "check your connection" and NOT an unexpected '
      'crash', () async {
    ConnectivityPlatform.instance = _FixedConnectivity(true);
    // Stands in for the OS aborting the socket mid-request (app backgrounded):
    // the http package surfaces it as a ClientException. The user is online, so
    // telling them to check their connection is wrong.
    final mockClient = MockClient((request) async {
      throw http.ClientException('Software caused connection abort', request.url);
    });

    final result = await http.runWithClient(
      () => _client().post('/chat/buddy-pills/refresh', const {}, (json) => json),
      () => mockClient,
    );

    expect(result.isFailure, isTrue);
    // connectionInterrupted uses the serverError code (honest, retryable).
    expect(result.errorOrNull?.code, ErrorCode.serverError);
    expect(result.errorOrNull?.code, isNot(ErrorCode.networkUnavailable));
    expect(result.errorOrNull?.code, isNot(ErrorCode.unexpected));
  });

  test('an online SocketException is a transit failure, not a connectivity loss',
      () async {
    ConnectivityPlatform.instance = _FixedConnectivity(true);
    final mockClient = MockClient((request) async {
      throw const SocketException('Connection refused');
    });

    final result = await http.runWithClient(
      () => _client().post('/chat/buddy-pills/refresh', const {}, (json) => json),
      () => mockClient,
    );

    expect(result.isFailure, isTrue);
    expect(result.errorOrNull?.code, ErrorCode.serverError);
    expect(result.errorOrNull?.code, isNot(ErrorCode.networkUnavailable));
  });

  test('an offline SocketException IS a genuine connectivity loss', () async {
    ConnectivityPlatform.instance = _FixedConnectivity(false);
    final mockClient = MockClient((request) async {
      throw const SocketException('Network is unreachable');
    });

    // _execute short-circuits to networkUnavailable when offline, which is the
    // honest "check your connection" case.
    final result = await http.runWithClient(
      () => _client().post('/chat/buddy-pills/refresh', const {}, (json) => json),
      () => mockClient,
    );

    expect(result.isFailure, isTrue);
    expect(result.errorOrNull?.code, ErrorCode.networkUnavailable);
  });
}
