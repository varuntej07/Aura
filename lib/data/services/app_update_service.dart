import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';
import 'package:shared_preferences/shared_preferences.dart';

/// Google Play update state relevant to Aura's user-facing flow.
enum AppUpdateState { none, available, downloaded }

/// Result of asking Google Play to open its flexible-update consent UI.
enum AppUpdateStartResult { accepted, cancelled }

class AppUpdateStatus {
  final AppUpdateState state;
  final int? availableVersionCode;
  final int? stalenessDays;
  final int priority;

  const AppUpdateStatus({
    required this.state,
    required this.availableVersionCode,
    required this.stalenessDays,
    required this.priority,
  });
}

/// Android-only adapter for Google Play's flexible in-app update flow.
///
/// The native Play API remains authoritative for whether this install/account
/// can update. Checks run after Aura's first frame and fail open, so Play Store
/// outages, sideloads, debug builds, and unsupported devices never delay or
/// prevent normal app use.
class AppUpdateService {
  AppUpdateService({
    required VoidCallback onUpdateDownloaded,
    required ValueChanged<int?> onUpdateFailed,
  }) : _onUpdateDownloaded = onUpdateDownloaded,
       _onUpdateFailed = onUpdateFailed {
    if (isSupported) _channel.setMethodCallHandler(_handleNativeEvent);
  }

  static const MethodChannel _channel = MethodChannel(
    'dev.varuntej.aura/app_update',
  );
  static const Duration _promptCooldown = Duration(hours: 24);
  static const String _lastPromptedVersionKey =
      'app_update_last_prompted_version';
  static const String _lastPromptedAtKey = 'app_update_last_prompted_at';

  final VoidCallback _onUpdateDownloaded;
  final ValueChanged<int?> _onUpdateFailed;

  bool get isSupported =>
      !kIsWeb && defaultTargetPlatform == TargetPlatform.android;

  Future<AppUpdateStatus> checkForUpdate() async {
    if (!isSupported) {
      return const AppUpdateStatus(
        state: AppUpdateState.none,
        availableVersionCode: null,
        stalenessDays: null,
        priority: 0,
      );
    }
    final payload = await _channel.invokeMapMethod<String, dynamic>(
      'checkForUpdate',
    );
    final state = switch (payload?['state']) {
      'available' => AppUpdateState.available,
      'downloaded' => AppUpdateState.downloaded,
      _ => AppUpdateState.none,
    };
    return AppUpdateStatus(
      state: state,
      availableVersionCode: payload?['availableVersionCode'] as int?,
      stalenessDays: payload?['stalenessDays'] as int?,
      priority: payload?['priority'] as int? ?? 0,
    );
  }

  /// Avoids showing the same optional update prompt on every app launch.
  Future<bool> shouldPromptFor(int? availableVersionCode) async {
    if (availableVersionCode == null) return true;
    final prefs = await SharedPreferences.getInstance();
    if (prefs.getInt(_lastPromptedVersionKey) != availableVersionCode) {
      return true;
    }
    final lastPromptedAt = prefs.getInt(_lastPromptedAtKey);
    if (lastPromptedAt == null) return true;
    final elapsed = DateTime.now().difference(
      DateTime.fromMillisecondsSinceEpoch(lastPromptedAt),
    );
    return elapsed >= _promptCooldown;
  }

  Future<void> recordPromptShown(int? availableVersionCode) async {
    if (availableVersionCode == null) return;
    final prefs = await SharedPreferences.getInstance();
    await prefs.setInt(_lastPromptedVersionKey, availableVersionCode);
    await prefs.setInt(
      _lastPromptedAtKey,
      DateTime.now().millisecondsSinceEpoch,
    );
  }

  Future<AppUpdateStartResult> startFlexibleUpdate() async {
    final result = await _channel.invokeMethod<String>('startFlexibleUpdate');
    return result == 'accepted'
        ? AppUpdateStartResult.accepted
        : AppUpdateStartResult.cancelled;
  }

  Future<void> completeFlexibleUpdate() async {
    await _channel.invokeMethod<void>('completeFlexibleUpdate');
  }

  void dispose() {
    if (isSupported) _channel.setMethodCallHandler(null);
  }

  Future<void> _handleNativeEvent(MethodCall call) async {
    switch (call.method) {
      case 'updateDownloaded':
        _onUpdateDownloaded();
        return;
      case 'updateFailed':
        final payload = call.arguments as Map<Object?, Object?>?;
        _onUpdateFailed(payload?['errorCode'] as int?);
        return;
    }
  }
}
