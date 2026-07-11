import 'dart:async';
import 'dart:convert';

import 'package:flutter/foundation.dart'
    show Uint8List;
import 'package:http/http.dart' as http;
import 'package:livekit_client/livekit_client.dart';
import 'package:permission_handler/permission_handler.dart';

import '../../core/constants/api_endpoints.dart';
import '../../core/errors/app_exception.dart';
import '../../core/logging/app_logger.dart';
import '../../core/voice/voice_error_copy.dart';
import '../../core/network/api_response.dart';
import '../models/voice_models.dart';
import 'analytics_service.dart';
import '../../core/analytics/analytics_client.dart';
import 'screen_wake_lock.dart';

const _tag = 'VoiceSession';

// How long we wait for Buddy to show ANY sign of life — the opening hello after
// the agent joins, or a reply after the user finishes talking — before we tell
// the user something's off instead of leaving them staring at a dead "Listening"
// screen. Reset on every signal from the agent (state change, audio, text), so a
// long tool call (e.g. web search) never trips it. Healthy first audio
// lands in 2-5s; 15s is a generous ceiling for "it's genuinely stuck".
const _replyWatchdogTimeout = Duration(seconds: 15);

// How long a token fetched at app-open stays usable for a tap-to-talk. 
// The backend mints LiveKit tokens with the SDK default 6h TTL, so this is a
// conservative reuse window that keeps us well clear of expiry while removing
// the token round-trip from the critical path when the user taps soon after.
const _prewarmedTokenTtl = Duration(minutes: 30);

class VoiceSessionService {
  final Future<String?> Function() _tokenProvider;
  final AnalyticsClient _postHogAnalyticsService;
  final ScreenWakeLock _screenWakeLock;

  Room? _room;
  EventsListener<RoomEvent>? _listener;
  Timer? _agentJoinWatchdog;
  Timer? _replyWatchdog;
  bool _isConnecting = false;
  bool _didEmitSessionReady = false;
  bool _didReceiveAssistantOutput = false;
  bool _awaitingAssistantReply = false;
  bool _didTrackFirstResponse = false;
  bool _closingByClient = false;
  // Token prefetched at app open (see prewarm), reused by startSession while fresh.
  Map<String, dynamic>? _prewarmedToken;
  DateTime? _prewarmedTokenAt;
  // On-screen context handed from the Buddy Keyboard, sent to the agent once it joins.
  // A few timed retries beat the agent's data-handler registration delay; the agent
  // dedupes repeats (one-shot). Session-lifecycle state, cleared on teardown.
  ScreenContextHandoff? _pendingScreenContext;
  bool _screenContextSent = false;
  Timer? _screenContextRetry1;
  Timer? _screenContextRetry2;
  final Stopwatch _sessionStopwatch = Stopwatch();
  final StreamController<VoiceServerEvent> _eventsController =
      StreamController<VoiceServerEvent>.broadcast();

  VoiceSessionService({
    required Future<String?> Function() tokenProvider,
    required AnalyticsClient postHogAnalyticsService,
    ScreenWakeLock? screenWakeLock,
  })  : _tokenProvider = tokenProvider,
        _postHogAnalyticsService = postHogAnalyticsService,
        _screenWakeLock = screenWakeLock ?? WakelockPlusScreenWakeLock();

  Stream<VoiceServerEvent> get events => _eventsController.stream;
  bool get isConnected => _room != null;

  /// Warm everything that can be done before the user taps the mic, so
  /// tap-to-talk doesn't pay for a token round-trip or an OS permission prompt.
  ///
  /// Fully fire-and-forget: every failure is swallowed so a cold backend or a
  /// denied mic at app open never surfaces an error or blocks the home screen.
  /// Safe to call repeatedly (e.g. on every home-screen mount).
  Future<void> prewarm() async {
    if (_room != null || _isConnecting) return;
    try {
      final idToken = await _tokenProvider();
      final token = await _fetchLiveKitToken(idToken);
      if (token != null) {
        _prewarmedToken = token;
        _prewarmedTokenAt = DateTime.now();
        AppLogger.info('Prewarmed LiveKit token', tag: _tag);
      }
    } catch (e) {
      AppLogger.warning('Voice prewarm: token prefetch failed', tag: _tag,
          metadata: {'error': e.toString()});
    }
    // Pre-resolve mic permission so the OS prompt is off the tap-to-talk path.
    // request() is idempotent, it only shows the dialog if undecided, and
    // returns immediately once granted or denied.
    try {
      await Permission.microphone.request();
    } catch (e) {
      AppLogger.warning('Voice prewarm: mic permission prefetch failed', tag: _tag,
          metadata: {'error': e.toString()});
    }
  }

  /// Return the prewarmed token if one was fetched recently enough to still be
  /// valid, otherwise null so the caller fetches a fresh one.
  Map<String, dynamic>? _freshPrewarmedToken() {
    final token = _prewarmedToken;
    final at = _prewarmedTokenAt;
    if (token == null || at == null) return null;
    if (DateTime.now().difference(at) > _prewarmedTokenTtl) {
      _prewarmedToken = null;
      _prewarmedTokenAt = null;
      return null;
    }
    return token;
  }

  Future<Result<void>> startSession(VoiceSessionConfig config) async {
    if (_room != null || _isConnecting) {
      AppLogger.warning('startSession called while already connected or connecting', tag: _tag);
      return const Result.success(null);
    }
    _isConnecting = true;
    _pendingScreenContext = config.screenContext;
    _screenContextSent = false;

    AppLogger.info('Requesting LiveKit token', tag: _tag,
        metadata: {'userId': config.userId});

    try {
      // Reuse the token prefetched at app open if it's still fresh;
      // otherwise fetch one now. A prewarmed token was minted WITHOUT a
      // surface (backend defaults it to "app"), so a session that declares
      // one must always fetch fresh or the agent would miss its surface note.
      var tokenResult = config.surface == null ? _freshPrewarmedToken() : null;
      if (tokenResult == null) {
        final idToken = await _tokenProvider();
        tokenResult = await _fetchLiveKitToken(idToken, surface: config.surface);
      }
      if (tokenResult == null) {
        return Result.failure(
          AppException.unexpected("Couldn't get Buddy on the line. Try again in a sec?"),
        );
      }

      final lkToken = tokenResult['token'] as String;
      final lkUrl = tokenResult['url'] as String;
      final roomName = tokenResult['room'] as String;

      _room = Room(roomOptions: const RoomOptions());
      _listener = _room!.createListener();

      _listener!
        ..on<RoomConnectedEvent>((_) {
          _didEmitSessionReady = true;
          _closingByClient = false;
          _sessionStopwatch.reset();
          _sessionStopwatch.start();
          // Keep the screen on for the whole call — the user watches the orb
          // without touching the screen, so the OS would otherwise sleep it.
          // Released in _cleanupRoom() on every teardown path.
          _acquireScreenWakeLock();
          AppLogger.info('LiveKit room connected', tag: _tag,
              metadata: {'room': roomName});
          unawaited(_postHogAnalyticsService.trackEvent('voice_session_started'));
          _eventsController.add(VoiceServerEvent(
            type: 'session.ready',
            sessionId: roomName,
          ));
          // Watchdog: if no agent joins within 20s the worker is likely scaled-down.
          _agentJoinWatchdog = Timer(const Duration(seconds: 20), () {
            if (_room != null && !_didReceiveAssistantOutput) {
              AppLogger.warning('Agent join timeout, no agent joined within 20s', tag: _tag);
              _emitSessionError(
                code: 'agent_join_timeout',
                message: "Buddy's taking too long to pick up. Give it another tap?",
              );
              close();
            }
          });
        })
        ..on<RoomDisconnectedEvent>((e) {
          final wasClientClose = _closingByClient;
          final endedBeforeAssistantOutput = _didEmitSessionReady && !_didReceiveAssistantOutput;
          final reason = e.reason?.toString() ?? 'unknown';
          AppLogger.info('LiveKit room disconnected', tag: _tag,
              metadata: {
                'room': roomName,
                'reason': reason,
                'clientInitiated': wasClientClose,
                'endedBeforeAssistantOutput': endedBeforeAssistantOutput,
              });
          if (wasClientClose) {
            _eventsController.add(const VoiceServerEvent(type: 'session.ended'));
          } else if (endedBeforeAssistantOutput) {
            _emitSessionError(
              code: 'agent_disconnected_early',
              message: "Call dropped before Buddy could say anything. Let's try again?",
              extra: {'reason': reason},
            );
          } else {
            _eventsController.add(const VoiceServerEvent(type: 'session.ended'));
          }
          _cleanupRoom();
        })
        ..on<ParticipantConnectedEvent>((e) {
          AppLogger.info('Remote participant joined room', tag: _tag,
              metadata: {'identity': e.participant.identity, 'kind': e.participant.kind.toString()});
          if (e.participant.kind == ParticipantKind.AGENT) {
            _agentJoinWatchdog?.cancel();
            _agentJoinWatchdog = null;
            // The agent's process is here, but it hasn't said hi yet. Watch for
            // the greeting — if it never comes (e.g. the LLM/TTS is down), this
            // is what saves the user from an endless "Listening" screen.
            _armReplyWatchdog(reason: 'awaiting_greeting');
            // The agent is now in the room: hand over any keyboard screen context.
            _scheduleScreenContextDelivery();
          }
        })
        ..on<ParticipantDisconnectedEvent>((e) {
          AppLogger.info('Remote participant left room', tag: _tag,
              metadata: {'identity': e.participant.identity});
        })
        ..on<ParticipantAttributesChanged>((e) {
          if (e.participant is RemoteParticipant) {
            final agentState = e.attributes['lk.agent.state'];
            if (agentState != null) {
              final mappedState = _mapAgentState(agentState);
              // Agent is actively thinking/talking — it's alive, so push the
              // silence watchdog back instead of letting it fire mid-tool-call.
              if (agentState == 'thinking' || agentState == 'speaking') {
                _pokeReplyWatchdog();
              }
              _eventsController.add(VoiceServerEvent(
                type: 'session.state',
                payload: {'state': mappedState},
              ));
              if (mappedState == 'error') {
                _emitSessionError(
                  code: 'agent_state_failed',
                  message: "Buddy hit a snag. Mind trying that again?",
                  extra: {'agent_state': agentState},
                );
              }
            }
          }
        })
        ..on<TrackSubscribedEvent>((e) {
          if (e.track is RemoteAudioTrack) {
            _markAssistantResponded();
            (e.track as RemoteAudioTrack).start();
            AppLogger.info('Remote audio track started', tag: _tag);
          }
        })
        ..on<TrackUnsubscribedEvent>((e) {
          if (e.track is RemoteAudioTrack) {
            (e.track as RemoteAudioTrack).stop();
          }
        })
        ..on<TranscriptionEvent>((e) {
          for (final seg in e.segments) {
            final isAssistant = e.participant is RemoteParticipant;
            final role = isAssistant ? 'assistant' : 'user';
            if (isAssistant) _markAssistantResponded();
            _eventsController.add(VoiceServerEvent(
              type: '$role.text.${seg.isFinal ? 'final' : 'delta'}',
              text: seg.text,
              sessionId: roomName,
            ));
            if (seg.isFinal && !isAssistant) {
              AppLogger.info('Voice user transcript final', tag: _tag);
              // User just finished talking — start the clock on Buddy's reply.
              _armReplyWatchdog(reason: 'awaiting_reply');
            }
          }
        })
        ..on<DataReceivedEvent>((e) => _handleDataMessage(e.data));

      // Buffer mic audio locally during connect so the user can talk the instant
      // they tap: withPreConnectAudio records before the agent is ready and ships
      // the buffer the moment Buddy goes active, discarding it if no agent joins.
      // The 25s readiness timeout sits just past the 20s agent-join watchdog so
      // the watchdog (with its friendly coded error) always owns the no-show case;
      // the room resets the buffer automatically on disconnect. A real
      // connect failure inside the operation still propagates to the catch below.
      await _room!.withPreConnectAudio(
        () async {
          await _room!.connect(
            lkUrl,
            lkToken,
            connectOptions: const ConnectOptions(autoSubscribe: true),
          );
          await _room!.localParticipant?.setMicrophoneEnabled(true);
        },
        timeout: const Duration(seconds: 25),
        onError: (e) => AppLogger.warning('Preconnect audio buffer error',
            tag: _tag, metadata: {'error': e.toString()}),
      );

      AppLogger.info('LiveKit mic enabled', tag: _tag);
      unawaited(AnalyticsService.logVoiceStarted());
      return const Result.success(null);
    } catch (e, st) {
      AppLogger.error('Failed to connect to LiveKit', error: e, stackTrace: st,
          tag: _tag, metadata: {'userId': config.userId});
      _cleanupRoom();
      final errorText = e.toString();
      final isIceFailure = errorText.contains('MediaConnectException') ||
          errorText.contains('PeerConnection');
      // Audio-capture failures (Windows privacy toggle, device busy, driver)
      // surface here as getUserMedia/media-device errors; give them actionable
      // copy instead of the generic line (failure mode #6).
      final isMicCaptureFailure = errorText.contains('getUserMedia') ||
          errorText.contains('MediaDevice') ||
          errorText.contains('audio renderer');
      return Result.failure(
        AppException.unexpected(
          isMicCaptureFailure
              ? voiceErrorMessageForCode(
                  code: micCaptureFailedCode, fallbackMessage: null)
              : isIceFailure
                  ? "Couldn't reach Buddy. Looks like a network hiccup. Try again?"
                  : "Couldn't start the call. Give it another shot in a sec?",
          error: e,
          stackTrace: st,
        ),
      );
    } finally {
      _isConnecting = false;
    }
  }

  /// Send text to the agent via data channel (used during active voice session).
  Future<Result<void>> sendTextInput(String text) async {
    final room = _room;
    if (room == null) {
      return Result.failure(AppException.unexpected('Voice session is not connected.'));
    }
    try {
      await room.localParticipant?.publishData(
        utf8.encode(jsonEncode({'type': 'text_input', 'text': text})),
        reliable: true,
      );
      return const Result.success(null);
    } catch (e, st) {
      return Result.failure(
        AppException.unexpected('Failed to send text input.', error: e, stackTrace: st),
      );
    }
  }

  /// Send one screen-sight JPEG to the agent as a byte stream (topic
  /// "screen_frame"). The writer chunks to 15KB packets with backpressure
  /// internally, so a whole frame goes in one write. Fail-soft: a dropped
  /// frame degrades to "Buddy can't see this turn", never a session error.
  Future<void> sendScreenFrame(
    Uint8List jpegBytes, {
    required Map<String, String> attributes,
  }) async {
    final participant = _room?.localParticipant;
    if (participant == null) return;
    try {
      final writer = await participant.streamBytes(StreamBytesOptions(
        topic: 'screen_frame',
        mimeType: 'image/jpeg',
        totalSize: jpegBytes.length,
        attributes: attributes,
      ));
      await writer.write(jpegBytes);
      await writer.close();
    } catch (e, st) {
      AppLogger.warning('Failed to send screen frame', tag: _tag,
          metadata: {'error': e.toString(), 'stackTrace': st.toString()});
    }
  }

  /// Send OCR-extracted text to the agent via data channel.
  Future<void> sendOcrContext(String text) async {
    final room = _room;
    if (room == null) return;
    try {
      await room.localParticipant?.publishData(
        utf8.encode(jsonEncode({'type': 'ocr_context', 'text': text})),
        reliable: true,
      );
    } catch (e, st) {
      AppLogger.warning('Failed to send OCR context', tag: _tag,
          metadata: {'error': e.toString(), 'stackTrace': st.toString()});
    }
  }

  /// Send the keyboard's screen context to the agent now and again a couple of times.
  /// The agent registers its data handler a beat after joining and dedupes repeats
  /// (one-shot), so the first send that lands after registration wins.
  void _scheduleScreenContextDelivery() {
    final ctx = _pendingScreenContext;
    if (ctx == null || _screenContextSent) return;
    _screenContextSent = true;
    unawaited(_sendScreenContext(ctx));
    _screenContextRetry1 =
        Timer(const Duration(milliseconds: 1200), () => unawaited(_sendScreenContext(ctx)));
    _screenContextRetry2 =
        Timer(const Duration(milliseconds: 2600), () => unawaited(_sendScreenContext(ctx)));
  }

  Future<void> _sendScreenContext(ScreenContextHandoff ctx) async {
    final room = _room;
    if (room == null) return;
    try {
      await room.localParticipant?.publishData(
        utf8.encode(jsonEncode({
          'type': 'screen_context',
          'context_before': ctx.text,
          if (ctx.fieldType != null) 'field_type': ctx.fieldType,
          if (ctx.app != null) 'app': ctx.app,
        })),
        reliable: true,
      );
    } catch (e, st) {
      AppLogger.warning('Failed to send screen context', tag: _tag,
          metadata: {'error': e.toString(), 'stackTrace': st.toString()});
    }
  }

  /// Disconnect from the room and emit session.ended.
  Future<void> close() async {
    AppLogger.info('Closing voice session', tag: _tag);
    _closingByClient = true;
    _sessionStopwatch.stop();
    unawaited(_postHogAnalyticsService.trackEvent(
      'voice_session_ended',
      properties: {'duration_seconds': _sessionStopwatch.elapsed.inSeconds},
    ));
    try {
      await _room?.disconnect();
    } catch (e) {
      // Disconnecting a half-dead room can throw; the session is ending anyway,
      // so we don't surface it — but we leave a breadcrumb so a stuck close is
      // traceable instead of vanishing.
      AppLogger.warning('Ignored error while disconnecting room', tag: _tag,
          metadata: {'error': e.toString()});
    }
    _cleanupRoom();
  }

  void _handleDataMessage(List<int> data) {
    try {
      final json = jsonDecode(utf8.decode(data)) as Map<String, dynamic>;
      final event = VoiceServerEvent.fromJson(json);
      AppLogger.debug('← data channel: ${event.type}', tag: _tag);
      // Any message from the agent is a sign of life — keep the watchdog at bay.
      _pokeReplyWatchdog();
      // The backend can push a session.error straight down the data channel when
      // its pipeline dies (e.g. all LLM providers exhausted). Treat it exactly
      // like a locally-detected error so it lands on the dashboard and stops the
      // watchdog from double-firing.
      if (event.type == 'session.error') {
        _awaitingAssistantReply = false;
        _replyWatchdog?.cancel();
        _replyWatchdog = null;
        final code = event.payload?['code'] as String? ?? 'backend_session_error';
        unawaited(_postHogAnalyticsService.trackEvent('voice_error',
            properties: {'code': code}));
      }
      _eventsController.add(event);
    } catch (e) {
      AppLogger.warning('Failed to parse data channel message', tag: _tag,
          metadata: {'error': e.toString()});
    }
  }

  /// Start (or restart) the silence watchdog. Fires once if the agent goes fully
  /// quiet for [_replyWatchdogTimeout] — no audio, no text, no state changes.
  void _armReplyWatchdog({required String reason}) {
    _awaitingAssistantReply = true;
    _replyWatchdog?.cancel();
    _replyWatchdog = Timer(_replyWatchdogTimeout, () {
      if (_room == null || !_awaitingAssistantReply) return;
      AppLogger.warning('Reply watchdog fired, Buddy went silent', tag: _tag,
          metadata: {'reason': reason});
      _emitSessionError(
        code: 'agent_silent',
        message: "Buddy's connected but gone quiet on me. Tap to try again?",
        extra: {'reason': reason},
      );
      close();
    });
  }

  /// Push the watchdog back when the agent shows a sign of life but hasn't
  /// delivered its reply yet (mid-thought, mid-tool-call).
  void _pokeReplyWatchdog() {
    if (!_awaitingAssistantReply) return;
    _armReplyWatchdog(reason: 'agent_active');
  }

  /// The agent actually produced output. Stop watching, and log the first
  /// success of the session so we can track voice reliability per user.
  void _markAssistantResponded() {
    _didReceiveAssistantOutput = true;
    _awaitingAssistantReply = false;
    _replyWatchdog?.cancel();
    _replyWatchdog = null;
    if (!_didTrackFirstResponse) {
      _didTrackFirstResponse = true;
      // _sessionStopwatch starts on RoomConnectedEvent, so this measures time
      // from room connect to Buddy's first output.
      unawaited(_postHogAnalyticsService.trackEvent(
        'voice_first_response',
        properties: {'elapsed_ms': _sessionStopwatch.elapsed.inMilliseconds},
      ));
    }
  }

  /// Surface a session-fatal error to the UI and record it for the dashboard.
  void _emitSessionError({
    required String code,
    required String message,
    Map<String, dynamic>? extra,
  }) {
    _awaitingAssistantReply = false;
    _replyWatchdog?.cancel();
    _replyWatchdog = null;
    unawaited(_postHogAnalyticsService.trackEvent('voice_error',
        properties: {'code': code}));
    _eventsController.add(VoiceServerEvent(
      type: 'session.error',
      message: message,
      payload: {'code': code, ...?extra},
    ));
  }

  String _mapAgentState(String agentState) {
    switch (agentState) {
      case 'listening':
        return 'listening';
      case 'thinking':
        return 'processing';
      case 'speaking':
        return 'speaking';
      case 'failed':
      case 'disconnected':
        return 'error';
      default:
        return 'listening';
    }
  }

  /// Ask the OS to keep the screen on for the duration of the call. A failure
  /// here must never break the session, so it's swallowed with a breadcrumb.
  void _acquireScreenWakeLock() {
    unawaited(_screenWakeLock.enable().catchError((Object e) {
      AppLogger.warning('Failed to acquire screen wake lock', tag: _tag,
          metadata: {'error': e.toString()});
    }));
  }

  /// Let the screen sleep again on its normal timer. Idempotent at the OS level,
  /// so calling it on a session that never acquired the lock is harmless.
  void _releaseScreenWakeLock() {
    unawaited(_screenWakeLock.disable().catchError((Object e) {
      AppLogger.warning('Failed to release screen wake lock', tag: _tag,
          metadata: {'error': e.toString()});
    }));
  }

  void _cleanupRoom() {
    _releaseScreenWakeLock();
    _agentJoinWatchdog?.cancel();
    _agentJoinWatchdog = null;
    _replyWatchdog?.cancel();
    _replyWatchdog = null;
    _screenContextRetry1?.cancel();
    _screenContextRetry1 = null;
    _screenContextRetry2?.cancel();
    _screenContextRetry2 = null;
    _pendingScreenContext = null;
    _screenContextSent = false;
    _listener?.dispose();
    _listener = null;
    _room = null;
    _didEmitSessionReady = false;
    _didReceiveAssistantOutput = false;
    _awaitingAssistantReply = false;
    _didTrackFirstResponse = false;
    _closingByClient = false;
  }

  Future<Map<String, dynamic>?> _fetchLiveKitToken(
    String? idToken, {
    String? surface,
  }) async {
    try {
      final resp = await http.get(
        Uri.parse(ApiEndpoints.voiceToken).replace(
          queryParameters: surface != null ? {'surface': surface} : null,
        ),
        headers: {
          'Content-Type': 'application/json',
          if (idToken != null) 'Authorization': 'Bearer $idToken',
        },
      ).timeout(const Duration(seconds: 10));
      if (resp.statusCode == 200) {
        return jsonDecode(resp.body) as Map<String, dynamic>;
      }
      AppLogger.error('Voice token request failed', tag: _tag,
          metadata: {'status': resp.statusCode});
      return null;
    } catch (e, st) {
      AppLogger.error('Voice token request error', error: e, stackTrace: st, tag: _tag);
      return null;
    }
  }

  Future<void> dispose() async {
    await close();
    await _eventsController.close();
  }
}
