import 'package:firebase_auth/firebase_auth.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:google_sign_in/google_sign_in.dart';
import 'package:mockito/annotations.dart';
import 'package:mockito/mockito.dart';

import 'package:aura/core/errors/app_exception.dart';
import 'package:aura/data/services/firebase_auth_service.dart';

import 'firebase_auth_service_apple_test.mocks.dart';

@GenerateNiceMocks([
  MockSpec<FirebaseAuth>(),
  MockSpec<GoogleSignIn>(),
  MockSpec<UserCredential>(),
  MockSpec<User>(),
])
void main() {
  late MockFirebaseAuth auth;
  late MockGoogleSignIn googleSignIn;
  late MockUserCredential userCredential;
  late MockUser user;
  late FirebaseAuthService service;

  setUp(() {
    auth = MockFirebaseAuth();
    googleSignIn = MockGoogleSignIn();
    userCredential = MockUserCredential();
    user = MockUser();
    service = FirebaseAuthService(auth: auth, googleSignIn: googleSignIn);
  });

  test('uses the native Apple provider with name and email scopes', () async {
    when(auth.signInWithProvider(any)).thenAnswer((_) async => userCredential);
    when(userCredential.user).thenReturn(user);
    when(user.uid).thenReturn('uid-apple');

    final result = await service.signInWithApple();

    expect(result.isSuccess, isTrue);
    expect(result.dataOrNull, same(user));
    final provider =
        verify(auth.signInWithProvider(captureAny)).captured.single
            as AppleAuthProvider;
    expect(provider.scopes, containsAll(<String>['email', 'name']));
  });

  test('maps a closed Apple sheet to authCancelled', () async {
    when(
      auth.signInWithProvider(any),
    ).thenThrow(FirebaseAuthException(code: 'canceled'));

    final result = await service.signInWithApple();

    expect(result.isFailure, isTrue);
    expect(result.errorOrNull?.code, ErrorCode.authCancelled);
  });

  test('maps an Apple sign-in network failure to the offline copy', () async {
    when(
      auth.signInWithProvider(any),
    ).thenThrow(FirebaseAuthException(code: 'network-request-failed'));

    final result = await service.signInWithApple();

    expect(result.isFailure, isTrue);
    expect(result.errorOrNull?.message, contains('offline'));
  });
}
