import 'dart:convert';
import 'dart:io';

import 'package:firebase_auth/firebase_auth.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:mockito/annotations.dart';
import 'package:mockito/mockito.dart';

import 'package:aura/data/services/desktop/pairing_service.dart';

import 'pairing_service_test.mocks.dart';

@GenerateNiceMocks([MockSpec<FirebaseAuth>(), MockSpec<UserCredential>()])
void main() {
  provideDummy<UserCredential>(MockUserCredential());

  late MockFirebaseAuth firebaseAuth;

  setUp(() {
    firebaseAuth = MockFirebaseAuth();
    when(firebaseAuth.signInWithCustomToken(any))
        .thenAnswer((_) async => MockUserCredential());
  });

  PairingService buildService(MockClient httpClient) {
    return PairingService(httpClient: httpClient, firebaseAuth: firebaseAuth);
  }

  test('happy path: normalizes the code, sends device_name, signs in',
      () async {
    late Map<String, dynamic> sentBody;
    final client = MockClient((request) async {
      sentBody = jsonDecode(request.body) as Map<String, dynamic>;
      return http.Response(jsonEncode({'custom_token': 'tok-123'}), 200);
    });

    final result = await buildService(client).claimAndSignIn('7q4k-2m9x');

    expect(result.isSuccess, isTrue);
    expect(sentBody['code'], '7Q4K2M9X');
    expect(sentBody['device_name'], isNotEmpty);
    verify(firebaseAuth.signInWithCustomToken('tok-123')).called(1);
  });

  test('400 maps to the expired-code copy and never touches Firebase',
      () async {
    final client = MockClient((_) async =>
        http.Response(jsonEncode({'error': 'invalid_or_expired'}), 400));

    final result = await buildService(client).claimAndSignIn('AAAABBBB');

    expect(result.isFailure, isTrue);
    expect(result.errorOrNull!.message, contains('expired'));
    verifyNever(firebaseAuth.signInWithCustomToken(any));
  });

  test('500 maps to a retry copy that does not blame the network', () async {
    final client = MockClient((_) async => http.Response('boom', 500));

    final result = await buildService(client).claimAndSignIn('AAAABBBB');

    expect(result.isFailure, isTrue);
    expect(result.errorOrNull!.message.toLowerCase(),
        isNot(contains('connection')));
  });

  test('network failure maps to connection copy', () async {
    final client =
        MockClient((_) async => throw const SocketException('offline'));

    final result = await buildService(client).claimAndSignIn('AAAABBBB');

    expect(result.isFailure, isTrue);
    expect(result.errorOrNull!.message, contains('connection'));
  });

  test('too-short code fails locally without any HTTP call', () async {
    var called = false;
    final client = MockClient((_) async {
      called = true;
      return http.Response('{}', 200);
    });

    final result = await buildService(client).claimAndSignIn('ABC');

    expect(result.isFailure, isTrue);
    expect(called, isFalse);
  });

  test('FirebaseAuthException after a valid claim mentions the PC clock',
      () async {
    when(firebaseAuth.signInWithCustomToken(any)).thenThrow(
        FirebaseAuthException(code: 'invalid-custom-token'));
    final client = MockClient((_) async =>
        http.Response(jsonEncode({'custom_token': 'tok-123'}), 200));

    final result = await buildService(client).claimAndSignIn('AAAABBBB');

    expect(result.isFailure, isTrue);
    expect(result.errorOrNull!.message.toLowerCase(), contains('time'));
  });
}
