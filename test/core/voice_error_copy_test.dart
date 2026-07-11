import 'package:flutter_test/flutter_test.dart';

import 'package:aura/core/voice/voice_error_copy.dart';

void main() {
  group('voiceErrorMessageForCode', () {
    const knownCodes = [
      'agent_join_timeout',
      'agent_silent',
      'agent_disconnected_early',
      'provider_unavailable',
      'agent_state_failed',
      'session_runtime_failed',
      'tts_pipeline_failed',
      'mic_permission_denied',
      micCaptureFailedCode,
    ];

    test('every known code maps to non-empty, non-generic copy', () {
      final generic = voiceErrorMessageForCode(code: null, fallbackMessage: null);
      for (final code in knownCodes) {
        final message =
            voiceErrorMessageForCode(code: code, fallbackMessage: null);
        expect(message, isNotEmpty, reason: code);
        expect(message, isNot(generic), reason: code);
      }
    });

    test('unknown code prefers the service-provided message', () {
      expect(
        voiceErrorMessageForCode(
            code: 'brand_new_code', fallbackMessage: 'specific detail'),
        'specific detail',
      );
    });

    test('unknown code with no fallback gets the generic line', () {
      expect(
        voiceErrorMessageForCode(code: 'brand_new_code', fallbackMessage: '  '),
        "Something went sideways with the call. Tap to try again?",
      );
    });

    test('mic capture copy avoids false certainty about the cause', () {
      final message = voiceErrorMessageForCode(
          code: micCaptureFailedCode, fallbackMessage: null);
      expect(message.toLowerCase(), contains('mic'));
      expect(message.toLowerCase(), contains('settings'));
    });
  });
}
