enum VoiceSessionStatus {
  disconnected,
  connecting,
  ready,
  listening,
  processing,
  speaking,
  ended,
  error,
}

enum VoiceTranscriptRole {
  user,
  assistant,
  tool,
}

class VoiceTranscriptEntry {
  final String id;
  final VoiceTranscriptRole role;
  final String text;
  final bool isFinal;

  const VoiceTranscriptEntry({
    required this.id,
    required this.role,
    required this.text,
    required this.isFinal,
  });

  VoiceTranscriptEntry copyWith({
    String? text,
    bool? isFinal,
  }) {
    return VoiceTranscriptEntry(
      id: id,
      role: role,
      text: text ?? this.text,
      isFinal: isFinal ?? this.isFinal,
    );
  }
}

class VoiceServerEvent {
  final String type;
  final String? sessionId;
  final String? message;
  final String? text;
  final String? toolName;
  final Map<String, dynamic>? payload;

  const VoiceServerEvent({
    required this.type,
    this.sessionId,
    this.message,
    this.text,
    this.toolName,
    this.payload,
  });

  factory VoiceServerEvent.fromJson(Map<String, dynamic> json) {
    return VoiceServerEvent(
      type: json['type'] as String? ?? 'unknown',
      sessionId: json['sessionId'] as String?,
      message: json['message'] as String?,
      text: json['text'] as String?,
      toolName: json['toolName'] as String?,
      payload: json['payload'] as Map<String, dynamic>?,
    );
  }
}

class VoiceSessionConfig {
  final String userId;

  /// On-screen / field text handed in from the Buddy Keyboard's Voice chip, sent to
  /// the agent as screen context once connected so Buddy can talk about what is on
  /// screen. Null for a normal mic tap or widget launch.
  final ScreenContextHandoff? screenContext;

  /// Launch surface stamped into the LiveKit token (`?surface=` on /voice/token)
  /// so the agent tailors its prompt (e.g. "desktop" renders the screen-sight
  /// section). Null means the backend default, "app".
  final String? surface;

  const VoiceSessionConfig({
    required this.userId,
    this.screenContext,
    this.surface,
  });
}

/// The on-screen text the keyboard handed to a voice session (the message/draft the
/// user was looking at), plus where it came from. Delivered to the agent over the
/// LiveKit data channel as a `screen_context` message.
class ScreenContextHandoff {
  final String text;
  final String? fieldType;
  final String? app;

  const ScreenContextHandoff({required this.text, this.fieldType, this.app});
}
