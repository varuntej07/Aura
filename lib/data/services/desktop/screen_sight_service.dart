import 'dart:async';
import 'dart:collection';

import 'package:flutter/foundation.dart';

import '../../../core/logging/app_logger.dart';
import '../../models/voice_models.dart';
import '../voice_session_service.dart';
import 'desktop_screen_capture_service.dart';
import 'pointing_overlay_service.dart';

const _tag = 'ScreenSight';

/// How many sent frames' geometry stays mappable. An element.point normally
/// references the newest frame; a couple extra absorb a fast turn overlap.
const _retainedFrameGeometryCount = 4;

/// Arm/disarm state and per-turn capture cadence for Buddy's screen sight.
///
/// Screen sight NEVER captures on its own (product decision: push-to-look, not
/// ambient watching): the user arms it per session via Ctrl+Alt+S or the eye
/// button on the panel, and pressing again disarms. While armed, one frame is
/// captured on arm and one at the start of each spoken turn (the first
/// user.text.delta), so the transfer rides in parallel with the user talking
/// and never delays the reply. Arming always ends with the session: it never
/// persists across sessions.
///
/// Created empty at boot (the hotkey wires before DI builds the voice stack)
/// and attached to its dependencies from the provider graph; toggling before
/// attach only flips the flag, so nothing can capture early.
class ScreenSightService extends ChangeNotifier {
  VoiceSessionService? _voiceService;
  DesktopScreenCaptureService? _captureService;
  PointingOverlayService? _pointingService;
  StreamSubscription<VoiceServerEvent>? _eventsSubscription;

  bool _armed = false;
  bool _capturedThisTurn = false;
  bool _sessionConnected = false;
  int _frameCounter = 0;

  /// Geometry of recently sent frames by frame_id, so an element.point event
  /// maps against the exact frame the model looked at.
  final LinkedHashMap<String, ScreenFrameGeometry> _sentFrameGeometry =
      LinkedHashMap();

  /// True while the user has granted Buddy sight for this session. The panel
  /// and pill MUST show a visible indicator whenever this is true.
  bool get armed => _armed;

  void attach({
    required VoiceSessionService voiceService,
    required DesktopScreenCaptureService captureService,
    PointingOverlayService? pointingService,
  }) {
    _voiceService = voiceService;
    _captureService = captureService;
    _pointingService = pointingService;
    _eventsSubscription?.cancel();
    _eventsSubscription = voiceService.events.listen(_handleVoiceEvent);
  }

  /// Ctrl+Alt+S / the eye button. Arming is the consent act; it captures the
  /// first frame immediately so "look at this" works in the same breath.
  void toggleArmed() {
    if (_armed) {
      disarm();
      return;
    }
    _armed = true;
    _capturedThisTurn = false;
    AppLogger.info('Screen sight armed', tag: _tag);
    notifyListeners();
    if (_sessionConnected || (_voiceService?.isConnected ?? false)) {
      unawaited(_captureAndSend(reason: 'armed'));
    }
  }

  void disarm() {
    if (!_armed) return;
    _armed = false;
    _capturedThisTurn = false;
    AppLogger.info('Screen sight disarmed', tag: _tag);
    notifyListeners();
  }

  void _handleVoiceEvent(VoiceServerEvent event) {
    switch (event.type) {
      case 'session.ready':
        _sessionConnected = true;
        // Arm survives the connect gap: user pressed the hotkey while the
        // session was still dialing, so deliver the first look now.
        if (_armed) unawaited(_captureAndSend(reason: 'session.ready'));

      case 'user.text.delta':
        if (_armed && !_capturedThisTurn) {
          _capturedThisTurn = true;
          unawaited(_captureAndSend(reason: 'turn'));
        }

      case 'user.text.final':
        _capturedThisTurn = false;

      case 'element.point':
        _handleElementPoint(event);

      case 'session.ended':
      case 'error':
      case 'session.error':
        _sessionConnected = false;
        // Consent is per-session by design; the next call starts blind.
        disarm();
    }
  }

  /// Buddy pointed at something: map the frame-space coordinates onto the
  /// real screen and hand the target to the pointing overlay.
  void _handleElementPoint(VoiceServerEvent event) {
    final pointingService = _pointingService;
    if (pointingService == null) return;
    final payload = event.payload;
    final x = payload?['x'];
    final y = payload?['y'];
    if (payload == null || x is! int || y is! int) {
      AppLogger.warning('element.point with unusable payload', tag: _tag,
          metadata: {'payload': payload.toString()});
      return;
    }
    final frameId = payload['frame_id'] as String? ?? '';
    final geometry =
        _sentFrameGeometry[frameId] ?? _sentFrameGeometry.values.lastOrNull;
    if (geometry == null) {
      // A point without any sent frame means the model hallucinated sight;
      // loudly skip rather than animate to garbage coordinates.
      AppLogger.warning('element.point with no known frame geometry',
          tag: _tag, metadata: {'frame_id': frameId});
      return;
    }
    unawaited(pointingService.pointAt(
      geometry: geometry,
      jpegX: x,
      jpegY: y,
      label: (payload['label'] as String? ?? '').trim(),
    ));
  }

  Future<void> _captureAndSend({required String reason}) async {
    final captureService = _captureService;
    final voiceService = _voiceService;
    if (captureService == null || voiceService == null) return;
    if (!_armed || !voiceService.isConnected) return;

    final frame = await captureService.captureCursorDisplay();
    if (frame == null || !_armed) return;

    _frameCounter += 1;
    final frameId = 'f$_frameCounter';
    _sentFrameGeometry[frameId] = frame.geometry;
    while (_sentFrameGeometry.length > _retainedFrameGeometryCount) {
      _sentFrameGeometry.remove(_sentFrameGeometry.keys.first);
    }
    await voiceService.sendScreenFrame(
      frame.jpegBytes,
      attributes: {
        'frame_id': frameId,
        'captured_at_ms': DateTime.now().millisecondsSinceEpoch.toString(),
        'jpeg_width_px': '${frame.jpegWidthPx}',
        'jpeg_height_px': '${frame.jpegHeightPx}',
        'monitor_left_px': '${frame.monitorLeftPx}',
        'monitor_top_px': '${frame.monitorTopPx}',
        'monitor_width_px': '${frame.monitorWidthPx}',
        'monitor_height_px': '${frame.monitorHeightPx}',
        'scale_factor': '${frame.scaleFactor}',
      },
    );
    AppLogger.info('Screen frame sent', tag: _tag, metadata: {
      'reason': reason,
      'frame_id': frameId,
      'bytes': frame.jpegBytes.length,
      'jpeg_px': '${frame.jpegWidthPx}x${frame.jpegHeightPx}',
    });
  }

  @override
  void dispose() {
    _eventsSubscription?.cancel();
    super.dispose();
  }
}
