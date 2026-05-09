import 'dart:async';

import '../../core/base/safe_change_notifier.dart';
import '../../core/errors/app_exception.dart';
import '../../core/errors/error_handler.dart';
import '../../core/logging/app_logger.dart';
import '../../data/models/chat_message_model.dart';
import '../../data/models/voice_models.dart';
import '../../data/repositories/chat_repository.dart';
import '../../data/services/notification_service.dart';
import '../../data/services/voice_session_service.dart';
import '../../data/services/wake_word_service.dart';

enum MicState { idle, listening, processing }

/// Manages voice sessions on the home screen.
/// Chat message persistence is done directly via [ChatRepository] so this
/// ViewModel stays small and focused on LiveKit / voice state only.
class HomeViewModel extends SafeChangeNotifier {
  final VoiceSessionService _voiceService;
  final WakeWordService _wakeWordService;
  final ChatRepository _chatRepository;
  final NotificationService _notificationService;

  StreamSubscription<VoiceServerEvent>? _voiceEventSub;
  StreamSubscription<EngagementTapPayload>? _engagementTapSub;
  StreamSubscription<AgentNudgeTapPayload>? _agentNudgeTapSub;

  MicState _micState = MicState.idle;
  VoiceSessionStatus _voiceStatus = VoiceSessionStatus.disconnected;
  AppException? _error;
  String _liveTranscript = ''; // assistant text streamed during a voice session
  final List<VoiceTranscriptEntry> _voiceTranscript = [];
  int _voiceTranscriptSequence = 0;
  String? _currentVoiceChatSessionId; // Drift session for persisting voice messages
  String? _currentUserId;

  // Deep-link routing callbacks set by HomeScreen — keeps GoRouter out of VM.
  void Function(EngagementTapPayload)? onEngagementTap;
  void Function(AgentNudgeTapPayload)? onAgentNudgeTap;

  HomeViewModel({
    required VoiceSessionService voiceSessionService,
    required WakeWordService wakeWordService,
    required ChatRepository chatRepository,
    required NotificationService notificationService,
  })  : _voiceService = voiceSessionService,
        _wakeWordService = wakeWordService,
        _chatRepository = chatRepository,
        _notificationService = notificationService {
    _voiceEventSub = _voiceService.events.listen(_handleVoiceEvent);
    _engagementTapSub =
        _notificationService.engagementTapStream.listen(_onEngagementTap);
    _agentNudgeTapSub =
        _notificationService.agentNudgeTapStream.listen(_onAgentNudgeTap);
  }

  // Getters 

  MicState get micState => _micState;
  VoiceSessionStatus get voiceStatus => _voiceStatus;
  AppException? get error => _error;
  String get liveTranscript => _liveTranscript;
  List<VoiceTranscriptEntry> get voiceTranscript => List.unmodifiable(_voiceTranscript);

  bool get hasActiveSession =>
      _voiceStatus != VoiceSessionStatus.disconnected &&
      _voiceStatus != VoiceSessionStatus.ended &&
      _voiceStatus != VoiceSessionStatus.error;

  // Voice session lifecycle 

  Future<void> initWakeWord(String userId) async {
    _currentUserId = userId;
    await _wakeWordService.start(() => startSession(userId));
    AppLogger.info('Wake word active', tag: 'HomeViewModel');
  }

  Future<void> startSession(String userId) async {
    _currentUserId = userId;
    if (hasActiveSession) return;

    _error = null;
    _voiceStatus = VoiceSessionStatus.connecting;
    _micState = MicState.listening;
    _liveTranscript = '';
    _voiceTranscript.clear();
    safeNotifyListeners();

    // Create a Drift session to persist voice messages so they appear in
    // Recent Chats in the drawer.
    try {
      _currentVoiceChatSessionId = await _chatRepository.createSession();
    } catch (e) {
      AppLogger.error('Failed to create voice chat session', error: e, tag: 'HomeViewModel');
    }

    final result = await _voiceService.startSession(
      VoiceSessionConfig(userId: userId),
    );

    await result.when(
      success: (_) async {
        ErrorHandler.logBreadcrumb('voice_session_started');
      },
      failure: (err) async {
        _error = err;
        _voiceStatus = VoiceSessionStatus.error;
        _micState = MicState.idle;
        safeNotifyListeners();
      },
    );
  }

  Future<void> stopSession() async {
    if (!hasActiveSession) return;
    await endSession();
  }

  Future<void> endSession() async {
    await _voiceService.close();
    _resetVoiceState();
    safeNotifyListeners();
  }

  Future<void> sendTextDuringVoice(String text) async {
    if (!hasActiveSession) return;
    _liveTranscript = '';
    _voiceStatus = VoiceSessionStatus.processing;
    _micState = MicState.processing;
    safeNotifyListeners();

    final result = await _voiceService.sendTextInput(text);
    if (result.errorOrNull != null) {
      _error = result.errorOrNull;
      _voiceStatus = VoiceSessionStatus.error;
      _micState = MicState.idle;
      safeNotifyListeners();
    }
  }

  void clearError() {
    _error = null;
    safeNotifyListeners();
  }

  // Private 

  void _onEngagementTap(EngagementTapPayload payload) {
    onEngagementTap?.call(payload);
  }

  void _onAgentNudgeTap(AgentNudgeTapPayload payload) {
    onAgentNudgeTap?.call(payload);
  }

  void _handleVoiceEvent(VoiceServerEvent event) {
    switch (event.type) {
      case 'session.ready':
        _voiceStatus = VoiceSessionStatus.ready;
        _micState = MicState.listening;
        _error = null;
        safeNotifyListeners();

      case 'session.state':
        final s = event.payload?['state'] as String?;
        if (s == 'listening') {
          _voiceStatus = VoiceSessionStatus.listening;
          _micState = MicState.listening;
        } else if (s == 'speaking') {
          _voiceStatus = VoiceSessionStatus.speaking;
          _micState = MicState.processing;
        } else if (s == 'processing') {
          _voiceStatus = VoiceSessionStatus.processing;
          _micState = MicState.processing;
        }
        safeNotifyListeners();

      case 'assistant.text.delta':
        _voiceStatus = VoiceSessionStatus.speaking;
        _liveTranscript = event.text ?? '';
        _upsertTranscript(
          role: VoiceTranscriptRole.assistant,
          text: _liveTranscript,
          isFinal: false,
        );
        safeNotifyListeners();

      case 'assistant.text.final':
        final text = (event.text ?? _liveTranscript).trim();
        if (text.isNotEmpty) unawaited(_saveVoiceMessage(text, isUser: false));
        _upsertTranscript(
          role: VoiceTranscriptRole.assistant,
          text: text,
          isFinal: true,
        );
        _liveTranscript = '';
        _voiceStatus = VoiceSessionStatus.ready;
        safeNotifyListeners();

      case 'user.text.delta':
        _voiceStatus = VoiceSessionStatus.listening;
        _micState = MicState.listening;
        _upsertTranscript(
          role: VoiceTranscriptRole.user,
          text: event.text ?? '',
          isFinal: false,
        );
        safeNotifyListeners();

      case 'user.text.final':
        final text = (event.text ?? '').trim();
        if (text.isNotEmpty) unawaited(_saveVoiceMessage(text, isUser: true));
        _upsertTranscript(
          role: VoiceTranscriptRole.user,
          text: text,
          isFinal: true,
        );
        _voiceStatus = VoiceSessionStatus.processing;
        _micState = MicState.processing;
        safeNotifyListeners();

      case 'tool_thinking':
      case 'tool.call':
      case 'tool_call':
        final text = (event.message ?? event.text ?? event.toolName ?? '').trim();
        if (text.isNotEmpty) {
          _appendTranscript(role: VoiceTranscriptRole.tool, text: text);
          safeNotifyListeners();
        }

      case 'error':
        _error = AppException.unexpected(event.message ?? 'Voice session error.');
        _voiceStatus = VoiceSessionStatus.error;
        _micState = MicState.idle;
        safeNotifyListeners();

      case 'session.ended':
        if (_liveTranscript.trim().isNotEmpty) {
          unawaited(_saveVoiceMessage(_liveTranscript.trim(), isUser: false));
          _upsertTranscript(
            role: VoiceTranscriptRole.assistant,
            text: _liveTranscript.trim(),
            isFinal: true,
          );
        }
        _liveTranscript = '';
        _resetVoiceState();
        safeNotifyListeners();
    }
  }

  void _upsertTranscript({
    required VoiceTranscriptRole role,
    required String text,
    required bool isFinal,
  }) {
    final trimmed = text.trim();
    if (trimmed.isEmpty) return;

    final lastIndex = _voiceTranscript.length - 1;
    if (lastIndex >= 0) {
      final last = _voiceTranscript[lastIndex];
      if (last.role == role && !last.isFinal) {
        _voiceTranscript[lastIndex] = last.copyWith(
          text: trimmed,
          isFinal: isFinal,
        );
        return;
      }
    }

    _voiceTranscript.add(VoiceTranscriptEntry(
      id: '${DateTime.now().microsecondsSinceEpoch}-${_voiceTranscriptSequence++}',
      role: role,
      text: trimmed,
      isFinal: isFinal,
    ));
  }

  void _appendTranscript({
    required VoiceTranscriptRole role,
    required String text,
  }) {
    final trimmed = text.trim();
    if (trimmed.isEmpty) return;
    _voiceTranscript.add(VoiceTranscriptEntry(
      id: '${DateTime.now().microsecondsSinceEpoch}-${_voiceTranscriptSequence++}',
      role: role,
      text: trimmed,
      isFinal: true,
    ));
  }

  Future<void> _saveVoiceMessage(String text, {required bool isUser}) async {
    if (_currentVoiceChatSessionId == null) return;
    final msg = ChatMessageModel(
      id: DateTime.now().microsecondsSinceEpoch.toString(),
      text: text,
      isUser: isUser,
      timestamp: DateTime.now(),
      channel: ChatMessageChannel.voice,
      sessionId: _currentVoiceChatSessionId,
    );
    await _chatRepository.saveMessage(msg, userId: _currentUserId);
  }

  void _resetVoiceState() {
    _voiceStatus = VoiceSessionStatus.disconnected;
    _micState = MicState.idle;
    _currentVoiceChatSessionId = null;
  }

  @override
  void dispose() {
    _voiceEventSub?.cancel();
    _engagementTapSub?.cancel();
    _agentNudgeTapSub?.cancel();
    unawaited(_wakeWordService.stop());
    unawaited(_voiceService.dispose());
    super.dispose();
  }
}
