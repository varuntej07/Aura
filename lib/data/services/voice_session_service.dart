import 'dart:async';
import 'dart:convert';

import 'package:http/http.dart' as http;
import 'package:livekit_client/livekit_client.dart';

import '../../core/constants/api_endpoints.dart';
import '../../core/errors/app_exception.dart';
import '../../core/logging/app_logger.dart';
import '../../core/network/api_response.dart';
import '../models/voice_models.dart';
import 'analytics_service.dart';

const _tag = 'VoiceSession';

class VoiceSessionService {
  final Future<String?> Function() _tokenProvider;

  Room? _room;
  EventsListener<RoomEvent>? _listener;
  bool _isConnecting = false;
  final StreamController<VoiceServerEvent> _eventsController =
      StreamController<VoiceServerEvent>.broadcast();

  VoiceSessionService({required Future<String?> Function() tokenProvider})
      : _tokenProvider = tokenProvider;

  Stream<VoiceServerEvent> get events => _eventsController.stream;
  bool get isConnected => _room != null;

  Future<Result<void>> startSession(VoiceSessionConfig config) async {
    if (_room != null || _isConnecting) {
      AppLogger.warning('startSession called while already connected or connecting', tag: _tag);
      return const Result.success(null);
    }
    _isConnecting = true;

    AppLogger.info('Requesting LiveKit token', tag: _tag,
        metadata: {'userId': config.userId});

    try {
      final idToken = await _tokenProvider();
      final tokenResult = await _fetchLiveKitToken(idToken);
      if (tokenResult == null) {
        return Result.failure(
          AppException.unexpected('Failed to obtain voice session token.'),
        );
      }

      final lkToken = tokenResult['token'] as String;
      final lkUrl = tokenResult['url'] as String;
      final roomName = tokenResult['room'] as String;

      _room = Room(roomOptions: const RoomOptions());
      _listener = _room!.createListener();

      _listener!
        ..on<RoomConnectedEvent>((_) {
          AppLogger.info('LiveKit room connected', tag: _tag,
              metadata: {'room': roomName});
          _eventsController.add(VoiceServerEvent(
            type: 'session.ready',
            sessionId: roomName,
          ));
        })
        ..on<RoomDisconnectedEvent>((e) {
          AppLogger.info('LiveKit room disconnected', tag: _tag,
              metadata: {'reason': e.reason?.toString()});
          _eventsController.add(const VoiceServerEvent(type: 'session.ended'));
          _cleanupRoom();
        })
        ..on<ParticipantConnectedEvent>((e) {
          AppLogger.info('Remote participant joined room', tag: _tag,
              metadata: {'identity': e.participant.identity, 'sid': e.participant.sid});
        })
        ..on<ParticipantDisconnectedEvent>((e) {
          AppLogger.info('Remote participant left room', tag: _tag,
              metadata: {'identity': e.participant.identity});
        })
        ..on<ParticipantAttributesChanged>((e) {
          if (e.participant is RemoteParticipant) {
            final agentState = e.attributes['lk.agent.state'];
            if (agentState != null) {
              _eventsController.add(VoiceServerEvent(
                type: 'session.state',
                payload: {'state': _mapAgentState(agentState)},
              ));
            }
          }
        })
        ..on<TrackSubscribedEvent>((e) {
          if (e.track is RemoteAudioTrack) {
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
            _eventsController.add(VoiceServerEvent(
              type: '$role.text.${seg.isFinal ? 'final' : 'delta'}',
              text: seg.text,
              sessionId: roomName,
            ));
            if (seg.isFinal && !isAssistant) {
              AppLogger.info('Voice user transcript final', tag: _tag);
            }
          }
        })
        ..on<DataReceivedEvent>((e) => _handleDataMessage(e.data));

      await _room!.connect(
        lkUrl,
        lkToken,
        connectOptions: const ConnectOptions(autoSubscribe: true),
      );

      await _room!.localParticipant?.setMicrophoneEnabled(true);

      AppLogger.info('LiveKit mic enabled', tag: _tag);
      unawaited(AnalyticsService.logVoiceStarted());
      return const Result.success(null);
    } catch (e, st) {
      AppLogger.error('Failed to connect to LiveKit', error: e, stackTrace: st,
          tag: _tag, metadata: {'userId': config.userId});
      _cleanupRoom();
      return Result.failure(
        AppException.unexpected(
          'Failed to connect to the voice session.',
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

  /// Disconnect from the room and emit session.ended.
  Future<void> close() async {
    AppLogger.info('Closing voice session', tag: _tag);
    try {
      await _room?.disconnect();
    } catch (_) {}
    _cleanupRoom();
  }

  void _handleDataMessage(List<int> data) {
    try {
      final json = jsonDecode(utf8.decode(data)) as Map<String, dynamic>;
      final event = VoiceServerEvent.fromJson(json);
      AppLogger.debug('← data channel: ${event.type}', tag: _tag);
      _eventsController.add(event);
    } catch (e) {
      AppLogger.warning('Failed to parse data channel message', tag: _tag,
          metadata: {'error': e.toString()});
    }
  }

  String _mapAgentState(String agentState) {
    switch (agentState) {
      case 'listening':
        return 'listening';
      case 'thinking':
        return 'processing';
      case 'speaking':
        return 'speaking';
      default:
        return 'listening';
    }
  }

  void _cleanupRoom() {
    _listener?.dispose();
    _listener = null;
    _room = null;
  }

  Future<Map<String, dynamic>?> _fetchLiveKitToken(String? idToken) async {
    try {
      final resp = await http.get(
        Uri.parse(ApiEndpoints.voiceToken),
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
