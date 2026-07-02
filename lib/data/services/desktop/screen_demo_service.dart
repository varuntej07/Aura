import 'dart:convert';

import 'package:flutter/foundation.dart';
import 'package:shared_preferences/shared_preferences.dart';

import '../../../core/logging/app_logger.dart';
import '../../../core/network/api_client.dart';
import 'desktop_screen_capture_service.dart';
import 'pointing_overlay_service.dart';

const _tag = 'ScreenDemo';

const screenDemoSeenPreferenceKey = 'desktop_screen_demo_seen';

/// One observation from POST /desktop/screen-demo.
class ScreenDemoObservation {
  final String comment;
  final int x;
  final int y;
  final String label;

  const ScreenDemoObservation({
    required this.comment,
    required this.x,
    required this.y,
    required this.label,
  });

  factory ScreenDemoObservation.fromJson(Map<String, dynamic> json) {
    return ScreenDemoObservation(
      comment: json['comment'] as String? ?? '',
      x: json['x'] as int? ?? 0,
      y: json['y'] as int? ?? 0,
      label: json['label'] as String? ?? '',
    );
  }
}

/// The first-look demo: Buddy glances at the screen ONCE and points at one
/// specific thing with a playful comment. Runs the first time a signed-in
/// user lands on the panel (the moment they learn screen sight exists), then
/// never again unless they replay it. The button press is the consent act for
/// this single capture; nothing here arms ongoing screen sight.
class ScreenDemoService extends ChangeNotifier {
  ScreenDemoService({
    required ApiClient apiClient,
    required DesktopScreenCaptureService captureService,
    required PointingOverlayService pointingService,
  })  : _apiClient = apiClient,
        _captureService = captureService,
        _pointingService = pointingService {
    SharedPreferences.getInstance().then((prefs) {
      _seen = prefs.getBool(screenDemoSeenPreferenceKey) ?? false;
      _resolvedSeenFlag = true;
      notifyListeners();
    });
  }

  final ApiClient _apiClient;
  final DesktopScreenCaptureService _captureService;
  final PointingOverlayService _pointingService;

  bool _seen = false;
  bool _resolvedSeenFlag = false;
  bool _running = false;

  /// True while the demo invitation card should show on the panel.
  bool get shouldOfferDemo => _resolvedSeenFlag && !_seen;
  bool get running => _running;

  Future<void> dismiss() => _markSeen();

  /// Capture once, ask the backend for something to point at, fly the buddy.
  Future<void> runDemo() async {
    if (_running) return;
    _running = true;
    notifyListeners();
    try {
      final frame = await _captureService.captureCursorDisplay();
      if (frame == null) {
        AppLogger.warning('Screen demo capture failed', tag: _tag);
        return;
      }
      final result = await _apiClient.post<ScreenDemoObservation>(
        '/desktop/screen-demo',
        {
          'jpeg_base64': base64Encode(frame.jpegBytes),
          'jpeg_width_px': frame.jpegWidthPx,
          'jpeg_height_px': frame.jpegHeightPx,
        },
        ScreenDemoObservation.fromJson,
      );
      await result.when(
        success: (observation) async {
          AppLogger.info('Screen demo observation', tag: _tag, metadata: {
            'label': observation.label,
            'comment': observation.comment,
          });
          await _pointingService.pointAt(
            geometry: frame.geometry,
            jpegX: observation.x,
            jpegY: observation.y,
            // The playful comment IS the bubble; the element label is
            // implicit in where the pointer lands.
            label: observation.comment,
          );
        },
        failure: (error) async {
          AppLogger.warning('Screen demo request failed', tag: _tag,
              metadata: {'error': error.message});
        },
      );
    } finally {
      _running = false;
      await _markSeen();
    }
  }

  Future<void> _markSeen() async {
    _seen = true;
    notifyListeners();
    final prefs = await SharedPreferences.getInstance();
    await prefs.setBool(screenDemoSeenPreferenceKey, true);
  }
}
