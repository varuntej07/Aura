import 'dart:async';

import 'package:cloud_firestore/cloud_firestore.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:mockito/annotations.dart';
import 'package:mockito/mockito.dart';

import 'package:aura/core/constants/app_constants.dart';
import 'package:aura/core/errors/app_exception.dart';
import 'package:aura/core/errors/network_exception.dart';
import 'package:aura/core/network/api_response.dart';
import 'package:aura/data/services/backend_api_service.dart';
import 'package:aura/presentation/viewmodels/link_device_viewmodel.dart';

import 'link_device_viewmodel_test.mocks.dart';

@GenerateNiceMocks([
  MockSpec<BackendApiService>(),
  MockSpec<FirebaseFirestore>(),
])
void main() {
  setUpAll(() {
    provideDummy<Result<Map<String, dynamic>>>(
      Result<Map<String, dynamic>>.failure(AppException.unexpected('dummy')),
    );
  });

  late MockBackendApiService backendApiService;
  late LinkDeviceViewModel vm;

  setUp(() {
    backendApiService = MockBackendApiService();
    // userId: null keeps loadLinkedDevices() a no-op, isolating the retry /
    // reconciliation logic under test. A mock Firestore (never stubbed, never
    // called) just needs to exist so the constructor doesn't touch the real
    // FirebaseFirestore.instance singleton, which isn't initialized in tests.
    vm = LinkDeviceViewModel(
      backendApiService: backendApiService,
      userId: null,
      firestore: MockFirebaseFirestore(),
    );
  });

  group('generateCode', () {
    test('retries once on a timeout and succeeds on the next attempt', () async {
      var attempt = 0;
      when(backendApiService.startDevicePairing()).thenAnswer((_) async {
        attempt++;
        if (attempt == 1) {
          return Result<Map<String, dynamic>>.failure(
              AppException.requestTimeout());
        }
        return const Result<Map<String, dynamic>>.success(
            {'code': 'ABCD2345', 'expires_in_seconds': 300});
      });

      await vm.generateCode();

      expect(attempt, 2);
      expect(vm.formattedCode, 'ABCD-2345');
      expect(vm.error, isNull);
    });

    test('gives up after maxApiRetries consecutive timeouts', () async {
      when(backendApiService.startDevicePairing()).thenAnswer(
        (_) async =>
            Result<Map<String, dynamic>>.failure(AppException.requestTimeout()),
      );

      await vm.generateCode();

      verify(backendApiService.startDevicePairing())
          .called(AppConstants.maxApiRetries);
      expect(vm.formattedCode, isNull);
      expect(vm.error, isNotNull);
    });

    test('does not retry a non-timeout failure', () async {
      when(backendApiService.startDevicePairing()).thenAnswer(
        (_) async => Result<Map<String, dynamic>>.failure(
            AppException.serverError(500, 'boom')),
      );

      await vm.generateCode();

      verify(backendApiService.startDevicePairing()).called(1);
      expect(vm.error, isNotNull);
    });
  });

  group('unlinkDevice', () {
    test('confirms immediately on success', () async {
      when(backendApiService.unlinkDevice('dev1')).thenAnswer(
        (_) async => const Result<Map<String, dynamic>>.success({'ok': true}),
      );

      final confirmed = await vm.unlinkDevice('dev1');

      expect(confirmed, isTrue);
      expect(vm.error, isNull);
      expect(vm.isUnlinking('dev1'), isFalse);
    });

    test('treats a 404 (already unlinked by an earlier attempt) as confirmed',
        () async {
      when(backendApiService.unlinkDevice('dev1')).thenAnswer(
        (_) async => Result<Map<String, dynamic>>.failure(
          const NetworkException(
            code: ErrorCode.notFound,
            message: 'Not found (404)',
            statusCode: 404,
          ),
        ),
      );

      final confirmed = await vm.unlinkDevice('dev1');

      expect(confirmed, isTrue);
      expect(vm.error, isNull);
    });

    test('retries an ambiguous timeout and confirms once it resolves',
        () async {
      var attempt = 0;
      when(backendApiService.unlinkDevice('dev1')).thenAnswer((_) async {
        attempt++;
        if (attempt == 1) {
          return Result<Map<String, dynamic>>.failure(
              AppException.requestTimeout());
        }
        return const Result<Map<String, dynamic>>.success({'ok': true});
      });

      final confirmed = await vm.unlinkDevice('dev1');

      expect(attempt, 2);
      expect(confirmed, isTrue);
    });

    test('does not retry and surfaces a real failure immediately', () async {
      when(backendApiService.unlinkDevice('dev1')).thenAnswer(
        (_) async => Result<Map<String, dynamic>>.failure(
            AppException.serverError(500, 'boom')),
      );

      final confirmed = await vm.unlinkDevice('dev1');

      verify(backendApiService.unlinkDevice('dev1')).called(1);
      expect(confirmed, isFalse);
      expect(vm.error, isNotNull);
    });

    test('isUnlinking is true only while a call for that device is in flight',
        () async {
      final gate = Completer<Result<Map<String, dynamic>>>();
      when(backendApiService.unlinkDevice('dev1'))
          .thenAnswer((_) => gate.future);

      final future = vm.unlinkDevice('dev1');
      expect(vm.isUnlinking('dev1'), isTrue);
      expect(vm.isUnlinking('dev2'), isFalse);

      gate.complete(const Result<Map<String, dynamic>>.success({'ok': true}));
      await future;

      expect(vm.isUnlinking('dev1'), isFalse);
    });
  });
}
