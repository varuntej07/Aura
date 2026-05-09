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

  const VoiceSessionConfig({required this.userId});
}
