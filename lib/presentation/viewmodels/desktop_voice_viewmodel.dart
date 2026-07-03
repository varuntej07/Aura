import 'dart:async';

import '../../core/base/safe_change_notifier.dart';
import '../../core/logging/app_logger.dart';
import '../../core/voice/voice_error_copy.dart';
import '../../data/models/chat_message_model.dart';
import '../../data/models/voice_models.dart';
import '../../data/repositories/chat_repository.dart';
import '../../data/services/desktop/overlay_controller.dart';
import '../../data/services/voice_session_service.dart';

const _tag = 'DesktopVoiceVM';

/// Voice session state for the desktop overlay. Deliberately slim (plan
/// decision): NOT HomeViewModel, whose eight stream subscriptions are
/// FCM-backed mobile surfaces that do not exist on Windows.
///
/// Mic live on summon (review decision 5): a hidden -> panel transition starts
/// a session immediately when signed in. Esc/hotkey dismissal ends it through
/// [OverlayController.onEndVoiceSession]; the controller's mic-visibility
/// invariant is fed from here via setVoiceActive.
///
/// M5 note: when autostart lands, boot must reveal the panel WITHOUT starting
/// the mic; give OverlayController a quiet reveal path then. During manual
/// dogfood, launching Buddy means you want to talk, so summon-starts-voice
/// applies to every path.
class DesktopVoiceViewModel extends SafeChangeNotifier {
  DesktopVoiceViewModel({
    required VoiceSessionService voiceSessionService,
    required ChatRepository chatRepository,
    required OverlayController overlayController,
    required String? Function() currentUserIdProvider,
    required Stream<String?> authUserIdStream,
  })  : _voiceService = voiceSessionService,
        _chatRepository = chatRepository,
        _overlayController = overlayController,
        _currentUserIdProvider = currentUserIdProvider {
    _lastPresentation = _overlayController.presentation;
    _overlayController.addListener(_onOverlayPresentationChanged);
    _overlayController.onEndVoiceSession = () => unawaited(endSession());
    _voiceEventsSubscription = _voiceService.events.listen(_handleVoiceEvent);
    _authUserIdSubscription = authUserIdStream.listen(_onAuthUserIdChanged);
  }

  final VoiceSessionService _voiceService;
  final ChatRepository _chatRepository;
  final OverlayController _overlayController;
  final String? Function() _currentUserIdProvider;

  StreamSubscription<VoiceServerEvent>? _voiceEventsSubscription;
  StreamSubscription<String?>? _authUserIdSubscription;
  late OverlayPresentation _lastPresentation;

  VoiceSessionStatus _status = VoiceSessionStatus.disconnected;
  String _assistantCaption = '';
  String _userCaption = '';
  String? _errorMessage;
  String? _chatSessionId;

  VoiceSessionStatus get status => _status;
  String get assistantCaption => _assistantCaption;
  String get userCaption => _userCaption;
  String? get errorMessage => _errorMessage;

  /// True when the current error is the capture failure whose fix lives in
  /// Windows Settings; the panel shows an "Open mic settings" shortcut.
  bool get showMicSettingsHint =>
      _errorMessage ==
      voiceErrorMessageForCode(
          code: micCaptureFailedCode, fallbackMessage: null);

  void _onOverlayPresentationChanged() {
    final presentation = _overlayController.presentation;
    final cameFromHidden = _lastPresentation == OverlayPresentation.hidden;
    _lastPresentation = presentation;
    if (presentation == OverlayPresentation.panel && cameFromHidden) {
      unawaited(startSession());
    }
  }

  Future<void> startSession() async {
    final userId = _currentUserIdProvider();
    if (userId == null) return; // signed out: the panel shows the sign-in form
    if (_voiceService.isConnected ||
        _status == VoiceSessionStatus.connecting) {
      return;
    }

    _status = VoiceSessionStatus.connecting;
    _errorMessage = null;
    _assistantCaption = '';
    _userCaption = '';
    safeNotifyListeners();
    _overlayController.setVoiceActive(true);

    try {
      _chatSessionId = await _chatRepository.createSession(userId: userId);
    } catch (e) {
      // Transcript persistence is best-effort; the call itself matters more.
      AppLogger.error('Failed to create desktop voice chat session',
          error: e, tag: _tag);
    }

    final result = await _voiceService.startSession(
        VoiceSessionConfig(userId: userId, surface: 'desktop'));
    result.when(
      success: (_) {},
      failure: (error) {
        _errorMessage = error.message;
        _status = VoiceSessionStatus.error;
        _overlayController.setVoiceActive(false);
        safeNotifyListeners();
      },
    );
  }

  Future<void> endSession() => _voiceService.close();

  /// Signing out must kill the call in the same gesture: without this, the
  /// panel flips to the sign-in form while the mic and the agent session stay
  /// live underneath it. Resets state directly rather than waiting for
  /// session.ended, which never arrives when the room hasn't finished
  /// connecting yet.
  void _onAuthUserIdChanged(String? userId) {
    if (userId != null) return;
    final nothingLive = !_voiceService.isConnected &&
        (_status == VoiceSessionStatus.disconnected ||
            _status == VoiceSessionStatus.ended);
    if (nothingLive) return;

    unawaited(endSession());
    _status = VoiceSessionStatus.disconnected;
    _assistantCaption = '';
    _userCaption = '';
    _errorMessage = null;
    _chatSessionId = null;
    _overlayController.setVoiceActive(false);
    safeNotifyListeners();
  }

  void _handleVoiceEvent(VoiceServerEvent event) {
    switch (event.type) {
      case 'session.ready':
        _status = VoiceSessionStatus.ready;
        _errorMessage = null;

      case 'session.state':
        final state = event.payload?['state'] as String?;
        if (state == 'listening') _status = VoiceSessionStatus.listening;
        if (state == 'speaking') _status = VoiceSessionStatus.speaking;
        if (state == 'processing') _status = VoiceSessionStatus.processing;

      case 'assistant.text.delta':
        _status = VoiceSessionStatus.speaking;
        _assistantCaption = event.text ?? '';

      case 'assistant.text.final':
        final text = (event.text ?? _assistantCaption).trim();
        _assistantCaption = text;
        if (text.isNotEmpty) unawaited(_saveVoiceMessage(text, isUser: false));
        _status = VoiceSessionStatus.ready;

      case 'user.text.delta':
        _status = VoiceSessionStatus.listening;
        _userCaption = event.text ?? '';

      case 'user.text.final':
        final text = (event.text ?? '').trim();
        _userCaption = text;
        if (text.isNotEmpty) unawaited(_saveVoiceMessage(text, isUser: true));
        _status = VoiceSessionStatus.processing;

      case 'error':
      case 'session.error':
        _errorMessage = voiceErrorMessageForCode(
          code: event.payload?['code'] as String?,
          fallbackMessage: event.message,
        );
        _status = VoiceSessionStatus.error;
        _overlayController.setVoiceActive(false);

      case 'session.ended':
        _status = VoiceSessionStatus.disconnected;
        _assistantCaption = '';
        _userCaption = '';
        _chatSessionId = null;
        _overlayController.setVoiceActive(false);

      default:
        return; // unknown event type: no state change, no rebuild
    }
    safeNotifyListeners();
  }

  Future<void> _saveVoiceMessage(String text, {required bool isUser}) async {
    final sessionId = _chatSessionId;
    if (sessionId == null) return;
    final message = ChatMessageModel(
      id: DateTime.now().microsecondsSinceEpoch.toString(),
      text: text,
      isUser: isUser,
      timestamp: DateTime.now(),
      channel: ChatMessageChannel.voice,
      sessionId: sessionId,
    );
    await _chatRepository.saveMessage(message,
        userId: _currentUserIdProvider());
  }

  @override
  void dispose() {
    _overlayController.removeListener(_onOverlayPresentationChanged);
    _voiceEventsSubscription?.cancel();
    _authUserIdSubscription?.cancel();
    unawaited(_voiceService.close());
    super.dispose();
  }
}
