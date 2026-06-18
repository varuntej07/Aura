import 'package:flutter/foundation.dart';
import 'package:permission_handler/permission_handler.dart';

import '../../core/logging/app_logger.dart';

/// Wake word detection stub — Picovoice Porcupine not yet configured.
///
/// **Setup (when ready):**
/// 1. Add `porcupine_flutter: ^3.0.5` back to pubspec.yaml
/// 2. Get a free access key at https://console.picovoice.ai
/// 3. Set [_accessKey] below
/// 4. For "Hey Buddy": download your custom .ppn file from Picovoice Console,
///    place it in assets/wake_word/hey_juno.ppn, then set [_useBuiltIn] = false
class WakeWordService {
  // TODO: Replace with your Picovoice access key from https://console.picovoice.ai
  static const String _accessKey = 'YOUR_PICOVOICE_ACCESS_KEY';

  // Set to false once you have the custom "Hey Buddy" .ppn file.
  // ignore: unused_field
  static const bool _useBuiltIn = true;

  bool _isRunning = false;

  bool get isRunning => _isRunning;

  /// Start listening. [onWakeWord] is called on the main thread when triggered.
  /// Currently a no-op stub — Porcupine not configured.
  Future<void> start(VoidCallback onWakeWord) async {
    if (_accessKey == 'YOUR_PICOVOICE_ACCESS_KEY') {
      AppLogger.warning(
        'WakeWordService: Picovoice access key not set, wake word disabled',
        tag: 'WakeWordService',
      );
      return;
    }

    final status = await Permission.microphone.request();
    if (!status.isGranted) {
      AppLogger.error(
        'Microphone permission denied, wake word disabled',
        tag: 'WakeWordService',
      );
      return;
    }

    // Porcupine integration removed until access key is configured.
    // Re-add porcupine_flutter dependency and implement here.
    AppLogger.info('Wake word service stub, not started', tag: 'WakeWordService');
  }

  Future<void> stop() async {
    if (!_isRunning) return;
    _isRunning = false;
    AppLogger.info('Wake word service stopped', tag: 'WakeWordService');
  }
}
