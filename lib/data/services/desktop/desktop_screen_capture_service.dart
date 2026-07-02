import 'package:flutter/services.dart';

import '../../../core/logging/app_logger.dart';

const _tag = 'ScreenCapture';

/// The geometry of one captured frame, retained (without the JPEG bytes) after
/// a frame is sent so an element.point event referencing it can be mapped back
/// onto the real screen.
class ScreenFrameGeometry {
  final int monitorLeftPx;
  final int monitorTopPx;
  final int monitorWidthPx;
  final int monitorHeightPx;
  final double scaleFactor;
  final int jpegWidthPx;
  final int jpegHeightPx;

  const ScreenFrameGeometry({
    required this.monitorLeftPx,
    required this.monitorTopPx,
    required this.monitorWidthPx,
    required this.monitorHeightPx,
    required this.scaleFactor,
    required this.jpegWidthPx,
    required this.jpegHeightPx,
  });
}

/// One captured frame from the native channel: the encoded JPEG plus the
/// geometry needed to map model coordinates back onto the real screen (the
/// pointing feature's coordinate chain starts from these numbers).
class CapturedScreenFrame {
  /// JPEG, downscaled to <=1280 long edge by the native side.
  final Uint8List jpegBytes;

  /// The captured monitor's rect in physical pixels, virtual-desktop coords.
  final int monitorLeftPx;
  final int monitorTopPx;
  final int monitorWidthPx;
  final int monitorHeightPx;

  /// The monitor's DPI scale (physical px / logical px), e.g. 1.5 at 150%.
  final double scaleFactor;

  /// Encoded JPEG dimensions (the coordinate space the vision model sees).
  final int jpegWidthPx;
  final int jpegHeightPx;

  const CapturedScreenFrame({
    required this.jpegBytes,
    required this.monitorLeftPx,
    required this.monitorTopPx,
    required this.monitorWidthPx,
    required this.monitorHeightPx,
    required this.scaleFactor,
    required this.jpegWidthPx,
    required this.jpegHeightPx,
  });

  ScreenFrameGeometry get geometry => ScreenFrameGeometry(
        monitorLeftPx: monitorLeftPx,
        monitorTopPx: monitorTopPx,
        monitorWidthPx: monitorWidthPx,
        monitorHeightPx: monitorHeightPx,
        scaleFactor: scaleFactor,
        jpegWidthPx: jpegWidthPx,
        jpegHeightPx: jpegHeightPx,
      );
}

/// Dart wrapper over the "aura/screen_capture" method channel implemented in
/// windows/runner/screen_capture_channel.cpp. Capture failures return null and
/// log loudly; screen sight degrades to "Buddy can't see" rather than erroring
/// the voice session.
class DesktopScreenCaptureService {
  static const _channel = MethodChannel('aura/screen_capture');

  /// Captures the display the cursor is currently on.
  Future<CapturedScreenFrame?> captureCursorDisplay() async {
    try {
      final raw = await _channel.invokeMethod<Map<Object?, Object?>>(
        'captureCursorDisplay',
      );
      if (raw == null) {
        AppLogger.warning('Screen capture returned no data', tag: _tag);
        return null;
      }
      return CapturedScreenFrame(
        jpegBytes: raw['jpeg_bytes']! as Uint8List,
        monitorLeftPx: raw['monitor_left_px']! as int,
        monitorTopPx: raw['monitor_top_px']! as int,
        monitorWidthPx: raw['monitor_width_px']! as int,
        monitorHeightPx: raw['monitor_height_px']! as int,
        scaleFactor: (raw['scale_factor']! as num).toDouble(),
        jpegWidthPx: raw['jpeg_width_px']! as int,
        jpegHeightPx: raw['jpeg_height_px']! as int,
      );
    } catch (e, st) {
      AppLogger.error('Screen capture failed',
          error: e, stackTrace: st, tag: _tag);
      return null;
    }
  }
}
