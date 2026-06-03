import 'package:wakelock_plus/wakelock_plus.dart';

/// Keeps the device screen awake for as long as it is held.
///
/// The voice session uses this so the display never sleeps mid-conversation:
/// the user is looking at the orb / transcript while talking, not touching the
/// screen, so the OS display-timeout would otherwise turn it off.
///
/// Abstracted so [VoiceSessionService] can be unit-tested with a fake that
/// records acquire/release without driving the real platform plugin.
abstract class ScreenWakeLock {
  /// Ask the OS to keep the screen on.
  Future<void> enable();

  /// Release the lock so the screen can sleep again on its normal timer.
  Future<void> disable();
}

/// Production [ScreenWakeLock] backed by the `wakelock_plus` plugin
/// (Android `FLAG_KEEP_SCREEN_ON`, iOS `isIdleTimerDisabled`).
class WakelockPlusScreenWakeLock implements ScreenWakeLock {
  @override
  Future<void> enable() => WakelockPlus.enable();

  @override
  Future<void> disable() => WakelockPlus.disable();
}
