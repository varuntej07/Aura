import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';

import '../../../core/logging/app_logger.dart';

const _tag = 'WindowEffects';

/// Dart side of the "aura/window_effects" method channel implemented in
/// windows/runner/window_effects_channel.cpp. Toggles DWM acrylic blur-behind
/// (the overlay's glass) per presentation: on for the panel and pill, off for
/// the fullscreen pointing flight, which must stay fully see-through.
///
/// Fail-soft: when the OS call is unavailable or fails, [glassSupported]
/// flips false and the overlay paints a near-opaque fallback surface instead
/// of translucent paint over an un-blurred desktop (unreadable, per M1
/// testing on transparent windows).
class WindowEffectsService extends ChangeNotifier {
  static const _channel = MethodChannel('aura/window_effects');

  bool? _glassSupported;

  /// Null until the first native call resolves; then whether acrylic applied.
  bool? get glassSupported => _glassSupported;

  Future<void> enableGlass({required Color tint}) => _setGlass(true, tint);

  Future<void> disableGlass() => _setGlass(false, const Color(0x00000000));

  Future<void> _setGlass(bool enabled, Color tint) async {
    try {
      final applied = await _channel.invokeMethod<bool>('setGlass', {
        'enabled': enabled,
        'tintArgb': tint.toARGB32(),
      });
      _updateSupported(applied ?? false);
    } catch (e) {
      AppLogger.warning('Window effects call failed',
          tag: _tag, metadata: {'enabled': enabled, 'error': e.toString()});
      _updateSupported(false);
    }
  }

  void _updateSupported(bool value) {
    if (_glassSupported == value) return;
    _glassSupported = value;
    notifyListeners();
  }
}
