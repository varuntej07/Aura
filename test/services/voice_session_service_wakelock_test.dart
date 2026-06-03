import 'package:flutter_test/flutter_test.dart';
import 'package:mockito/annotations.dart';

import 'package:aura/data/services/posthog_analytics_service.dart';
import 'package:aura/data/services/screen_wake_lock.dart';
import 'package:aura/data/services/voice_session_service.dart';

import 'voice_session_service_wakelock_test.mocks.dart';

/// The voice session holds a screen wake lock so the display can't sleep
/// mid-call. The invariant that matters for battery is the *release*: when a
/// session ends through any path it funnels into `_cleanupRoom()`, which must
/// drop the lock so the screen sleeps again. These tests pin that down, plus
/// the rule that a wake-lock failure can never break tearing a session down.
///
/// The acquire path fires inside LiveKit's RoomConnectedEvent, which needs a
/// live room and so isn't unit-testable here — it's verified by review.
@GenerateNiceMocks([MockSpec<PostHogAnalyticsService>()])
void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  VoiceSessionService buildService(ScreenWakeLock wakeLock) {
    return VoiceSessionService(
      tokenProvider: () async => 'test-token',
      postHogAnalyticsService: MockPostHogAnalyticsService(),
      screenWakeLock: wakeLock,
    );
  }

  test('close() releases the screen wake lock exactly once', () async {
    final wakeLock = _RecordingScreenWakeLock();
    final sut = buildService(wakeLock);

    await sut.close();

    expect(wakeLock.disableCount, 1,
        reason: 'closing the session must let the screen sleep again');
  });

  test('close() does not throw when releasing the wake lock fails', () async {
    final wakeLock = _ThrowingOnDisableScreenWakeLock();
    final sut = buildService(wakeLock);

    // A dead/disabled wake-lock plugin must never block session teardown.
    await expectLater(sut.close(), completes);
    expect(wakeLock.disableAttempted, isTrue);
  });
}

class _RecordingScreenWakeLock implements ScreenWakeLock {
  int enableCount = 0;
  int disableCount = 0;

  @override
  Future<void> enable() async => enableCount++;

  @override
  Future<void> disable() async => disableCount++;
}

class _ThrowingOnDisableScreenWakeLock implements ScreenWakeLock {
  bool disableAttempted = false;

  @override
  Future<void> enable() async {}

  @override
  Future<void> disable() async {
    disableAttempted = true;
    throw StateError('wake lock plugin unavailable');
  }
}
