/// Voice error copy shared by mobile (HomeViewModel) and desktop
/// (DesktopVoiceViewModel), so a call falling over reads identically on every
/// surface. Casual, blame-the-tech-not-the-user, always points at the retry.
library;

/// Emitted when audio capture fails at connect time on desktop. Deliberately
/// vague about the cause: a capture failure can be the Windows privacy toggle,
/// the device being busy, or a driver issue, and the copy must not claim
/// false certainty (eng review, failure mode #6).
const String micCaptureFailedCode = 'mic_capture_failed';

String voiceErrorMessageForCode({
  required String? code,
  required String? fallbackMessage,
}) {
  switch (code) {
    case 'agent_join_timeout':
      return "Buddy's taking too long to pick up. Give it another tap?";
    case 'agent_silent':
      return "Buddy's connected but gone quiet on me. Tap to try again?";
    case 'agent_disconnected_early':
      return "Call dropped before Buddy could say anything. Let's try again?";
    case 'provider_unavailable':
      return "Buddy's voice is having a moment on our end. Hang tight and try again shortly.";
    case 'agent_state_failed':
    case 'session_runtime_failed':
    case 'tts_pipeline_failed':
      return "Buddy hit a snag mid-call. Mind tapping to start over?";
    case 'mic_permission_denied':
      return "I need mic access to hear you. Flip it on in Settings and tap again.";
    case micCaptureFailedCode:
      return "Couldn't access your mic. Check it's plugged in and allowed in Settings, then try again.";
    default:
      // Prefer whatever specific message the service handed us; only fall
      // back to a generic line if there's genuinely nothing better.
      final message = fallbackMessage?.trim();
      return (message != null && message.isNotEmpty)
          ? message
          : "Something went sideways with the call. Tap to try again?";
  }
}
