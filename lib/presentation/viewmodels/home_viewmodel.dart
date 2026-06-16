import 'dart:async';

import '../../core/base/safe_change_notifier.dart';
import '../../core/errors/app_exception.dart';
import '../../core/errors/error_handler.dart';
import '../../core/logging/app_logger.dart';
import '../../data/models/chat_message_model.dart';
import '../../data/models/voice_models.dart';
import '../../data/repositories/chat_repository.dart';
import '../../data/services/app_feedback_service.dart';
import '../../data/services/buddy_pills_refresher.dart';
import '../../data/services/notification_service.dart';
import '../../data/services/voice_session_service.dart';
import '../../data/services/wake_word_service.dart';

enum MicState { idle, listening, processing }

/// Snapshot of a just-ended voice call, used to render the "Voice chat ended"
/// rating card. Held after the live session state is reset.
class VoiceSessionEndedSummary {
  final String? sessionId;
  final Duration duration;

  const VoiceSessionEndedSummary({
    required this.sessionId,
    required this.duration,
  });
}

/// Manages voice sessions on the home screen.
/// Chat message persistence is done directly via [ChatRepository] so this
/// ViewModel stays small and focused on LiveKit / voice state only.
class HomeViewModel extends SafeChangeNotifier {
  final VoiceSessionService _voiceService;
  final WakeWordService _wakeWordService;
  final ChatRepository _chatRepository;
  final NotificationService _notificationService;
  final AppFeedbackService _appFeedbackService;
  final BuddyPillsRefresher _buddyPillsRefresher;

  StreamSubscription<VoiceServerEvent>? _voiceEventSub;
  StreamSubscription<EngagementTapPayload>? _engagementTapSub;
  StreamSubscription<AgentNudgeTapPayload>? _agentNudgeTapSub;
  StreamSubscription<SignalNotificationTapPayload>? _signalTapSub;
  StreamSubscription<ThreadFollowUpTapPayload>? _threadTapSub;
  StreamSubscription<IcebreakerTapPayload>? _icebreakerTapSub;
  StreamSubscription<DailyBriefingTapPayload>? _briefingTapSub;
  StreamSubscription<TrackerUpdateTapPayload>? _trackerUpdateTapSub;

  MicState _micState = MicState.idle;
  VoiceSessionStatus _voiceStatus = VoiceSessionStatus.disconnected;
  AppException? _error;
  String _liveTranscript = ''; // assistant text streamed during a voice session
  final List<VoiceTranscriptEntry> _voiceTranscript = [];
  int _voiceTranscriptSequence = 0;
  String? _currentVoiceChatSessionId; // Drift session for persisting voice messages
  String? _currentUserId;
  DateTime? _sessionStartedAt; // for the ended-call duration
  VoiceSessionEndedSummary? _endedSummary;

  // Deep-link routing callbacks set by HomeScreen — keeps GoRouter out of VM.
  void Function(EngagementTapPayload)? onEngagementTap;
  void Function(AgentNudgeTapPayload)? onAgentNudgeTap;
  void Function(SignalNotificationTapPayload)? onSignalNotificationTap;
  void Function(ThreadFollowUpTapPayload)? onThreadFollowUpTap;
  void Function(IcebreakerTapPayload)? onIcebreakerTap;
  void Function(DailyBriefingTapPayload)? onDailyBriefingTap;
  void Function(TrackerUpdateTapPayload)? onTrackerUpdateTap;

  HomeViewModel({
    required VoiceSessionService voiceSessionService,
    required WakeWordService wakeWordService,
    required ChatRepository chatRepository,
    required NotificationService notificationService,
    required AppFeedbackService appFeedbackService,
    required BuddyPillsRefresher buddyPillsRefresher,
  })  : _voiceService = voiceSessionService,
        _wakeWordService = wakeWordService,
        _chatRepository = chatRepository,
        _notificationService = notificationService,
        _appFeedbackService = appFeedbackService,
        _buddyPillsRefresher = buddyPillsRefresher {
    _voiceEventSub = _voiceService.events.listen(_handleVoiceEvent);
    _engagementTapSub = _notificationService.engagementTapStream.listen(_onEngagementTap);
    _agentNudgeTapSub = _notificationService.agentNudgeTapStream.listen(_onAgentNudgeTap);
    _signalTapSub = _notificationService.signalNotificationTapStream.listen(_onSignalNotificationTap);
    _threadTapSub = _notificationService.threadFollowUpTapStream.listen(_onThreadFollowUpTap);
    _icebreakerTapSub = _notificationService.icebreakerTapStream.listen(_onIcebreakerTap);
    _briefingTapSub = _notificationService.dailyBriefingTapStream.listen(_onDailyBriefingTap);
    _trackerUpdateTapSub = _notificationService.trackerUpdateTapStream.listen(_onTrackerUpdateTap);
  }

  // Getters 

  MicState get micState => _micState;
  VoiceSessionStatus get voiceStatus => _voiceStatus;
  AppException? get error => _error;
  String get liveTranscript => _liveTranscript;
  List<VoiceTranscriptEntry> get voiceTranscript => List.unmodifiable(_voiceTranscript);
  VoiceSessionEndedSummary? get endedSummary => _endedSummary;

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

  /// Warm the voice stack (LiveKit token + mic permission) when the home screen
  /// mounts, so tapping the mic connects without a token round-trip or a
  /// permission prompt in the way. Fire-and-forget; the service swallows any
  /// failure so a cold backend or denied mic never reaches the UI.
  Future<void> prewarmVoice() => _voiceService.prewarm();

  Future<void> startSession(String userId) async {
    _currentUserId = userId;
    if (hasActiveSession) return;

    // A voice session is real activity too, ground the next Buddy-pills
    // regeneration on it so a voice-only user still gets fresh pills.
    _buddyPillsRefresher.markActivity();

    _error = null;
    _voiceStatus = VoiceSessionStatus.connecting;
    _micState = MicState.listening;
    _liveTranscript = '';
    _voiceTranscript.clear();
    _sessionStartedAt = DateTime.now();
    _endedSummary = null;
    safeNotifyListeners();

    // Create a Drift session to persist voice messages so they appear in
    // Recent Chats in the drawer.
    try {
      _currentVoiceChatSessionId = await _chatRepository.createSession(
        userId: _currentUserId ?? '',
      );
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
    final sessionId = _currentVoiceChatSessionId;
    await _voiceService.close();
    if (sessionId != null) {
      unawaited(_saveVoiceSessionTitle(sessionId));
    }
    _captureEndedSummary(sessionId);
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

  void _onSignalNotificationTap(SignalNotificationTapPayload payload) {
    onSignalNotificationTap?.call(payload);
  }

  /// Reports a "read" signal notification's article open (vector nudge + funnel read-terminal). 
  /// Delegates to the notification service so the screen stays out of the service layer.
  Future<void> reportSignalContentOpened(SignalNotificationTapPayload payload) {
    return _notificationService.reportContentOpened(
      contentId: payload.contentId,
      category: payload.category,
      notificationId: payload.notificationId,
    );
  }

  void _onThreadFollowUpTap(ThreadFollowUpTapPayload payload) {
    onThreadFollowUpTap?.call(payload);
  }

  void _onIcebreakerTap(IcebreakerTapPayload payload) {
    onIcebreakerTap?.call(payload);
  }

  void _onDailyBriefingTap(DailyBriefingTapPayload payload) {
    onDailyBriefingTap?.call(payload);
  }

  void _onTrackerUpdateTap(TrackerUpdateTapPayload payload) {
    onTrackerUpdateTap?.call(payload);
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
        _updateOrInsertTranscriptEntry(
          role: VoiceTranscriptRole.assistant,
          text: _liveTranscript,
          isFinal: false,
        );
        safeNotifyListeners();

      case 'assistant.text.final':
        final text = (event.text ?? _liveTranscript).trim();
        if (text.isNotEmpty) unawaited(_saveVoiceMessage(text, isUser: false));
        _updateOrInsertTranscriptEntry(
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
        _updateOrInsertTranscriptEntry(
          role: VoiceTranscriptRole.user,
          text: event.text ?? '',
          isFinal: false,
        );
        safeNotifyListeners();

      case 'user.text.final':
        final text = (event.text ?? '').trim();
        if (text.isNotEmpty) unawaited(_saveVoiceMessage(text, isUser: true));
        _updateOrInsertTranscriptEntry(
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
        _error = AppException.unexpected(
          _toVoiceErrorMessage(
            code: event.payload?['code'] as String?,
            fallbackMessage: event.message,
          ),
        );
        _voiceStatus = VoiceSessionStatus.error;
        _micState = MicState.idle;
        safeNotifyListeners();

      case 'session.error':
        _error = AppException.unexpected(
          _toVoiceErrorMessage(
            code: event.payload?['code'] as String?,
            fallbackMessage: event.message,
          ),
        );
        _voiceStatus = VoiceSessionStatus.error;
        _micState = MicState.idle;
        safeNotifyListeners();

      case 'session.ended':
        if (_liveTranscript.trim().isNotEmpty) {
          unawaited(_saveVoiceMessage(_liveTranscript.trim(), isUser: false));
          _updateOrInsertTranscriptEntry(
            role: VoiceTranscriptRole.assistant,
            text: _liveTranscript.trim(),
            isFinal: true,
          );
        }
        _liveTranscript = '';
        final endedSessionId = _currentVoiceChatSessionId;
        if (endedSessionId != null) {
          unawaited(_saveVoiceSessionTitle(endedSessionId));
        }
        _captureEndedSummary(endedSessionId);
        _resetVoiceState();
        safeNotifyListeners();
    }
  }

  /// Maps a voice error code to copy a real person would actually want to read
  /// when a call falls over. Casual, blame-the-tech-not-the-user, always ends
  /// with a clear "tap to try again" since the mic orb is the retry button.
  String _toVoiceErrorMessage({
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
        return "I need mic access to hear you — flip it on in Settings and tap again.";
      default:
        // Prefer whatever specific message the service handed us; only fall
        // back to a generic line if there's genuinely nothing better.
        final msg = fallbackMessage?.trim();
        return (msg != null && msg.isNotEmpty)
            ? msg
            : "Something went sideways with the call. Tap to try again?";
    }
  }

  void _updateOrInsertTranscriptEntry({
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

  Future<void> _saveVoiceSessionTitle(String sessionId) async {
    final title = _deriveTitleFromVoiceTranscript();
    if (title.isEmpty) return;
    await _chatRepository.setSessionTitle(sessionId, title, userId: _currentUserId);
  }

  String _deriveTitleFromVoiceTranscript() {
    for (final entry in _voiceTranscript) {
      if (entry.role == VoiceTranscriptRole.user && entry.text.trim().isNotEmpty) {
        final text = entry.text.trim();
        return text.length > 60 ? '${text.substring(0, 57)}...' : text;
      }
    }
    return '';
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

  // Snapshot the just-ended call so the "Voice chat ended" rating card can
  // render after live state is reset.
  void _captureEndedSummary(String? sessionId) {
    final start = _sessionStartedAt;
    _endedSummary = VoiceSessionEndedSummary(
      sessionId: sessionId,
      duration: start == null ? Duration.zero : DateTime.now().difference(start),
    );
  }

  /// Dismisses the ended-call rating card 
  void dismissEndedSummary() {
    if (_endedSummary == null) return;
    _endedSummary = null;
    safeNotifyListeners();
  }

  /// Records a like/dislike on the just-ended voice call to the shared
  /// `app_feedback` collection. Returns null on success, or a user-facing
  /// error message on failure.
  Future<String?> submitVoiceSessionRating({
    required bool liked,
    List<String> reasons = const [],
    String? note,
  }) async {
    final uid = _currentUserId;
    if (uid == null || uid.isEmpty) {
      return "You're signed out. Sign back in to send feedback.";
    }
    final summary = _endedSummary;
    final rating = liked ? 'like' : 'dislike';
    return _appFeedbackService.submit(
      uid: uid,
      category: 'voice',
      text: note ?? '',
      extraFields: {
        'rating': rating,
        'reasons': reasons,
        'duration_seconds': summary?.duration.inSeconds ?? 0,
        'session_id': summary?.sessionId ?? '',
        'source': 'voice_session_end',
      },
      extraEventProperties: {
        'rating': rating,
        'source': 'voice_session_end',
      },
    );
  }

  @override
  void dispose() {
    _voiceEventSub?.cancel();
    _engagementTapSub?.cancel();
    _agentNudgeTapSub?.cancel();
    _signalTapSub?.cancel();
    _threadTapSub?.cancel();
    _icebreakerTapSub?.cancel();
    _briefingTapSub?.cancel();
    _trackerUpdateTapSub?.cancel();
    unawaited(_wakeWordService.stop());
    unawaited(_voiceService.dispose());
    super.dispose();
  }
}
