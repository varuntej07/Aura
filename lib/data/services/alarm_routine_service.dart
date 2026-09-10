import 'package:geolocator/geolocator.dart';

import '../../core/logging/app_logger.dart';
import '../../core/network/api_client.dart';

/// The post-alarm routine config the alarm page and Routines editor round-trip
/// with the backend, and the morning brief fetched after "I'm up".
///
/// Config lives server-side (`users/{uid}/settings/alarm_routine`) rather than
/// in the native alarm prefs because nothing here is needed at ring time: the
/// brief belongs to the moment the user is back in the app, network in hand.
///
/// Location: the weather section wants approximate coordinates. They are read
/// in the foreground only, right before the one brief request, and are never
/// stored. Denied permission (or no fix) simply means regional weather.
class AlarmRoutineService {
  AlarmRoutineService({required ApiClient apiClient}) : _apiClient = apiClient;

  final ApiClient _apiClient;

  static const String _tag = 'AlarmRoutineService';

  /// Server-owned action order for a fresh config; mirrored from the backend's
  /// VALID_ACTION_TYPES so an offline first open still renders the editor.
  static const List<String> defaultActions = [
    'weather',
    'calendar',
    'tasks',
    'joke',
  ];

  static const Duration _briefTimeout = Duration(seconds: 8);
  static const Duration _locationTimeout = Duration(seconds: 3);

  Future<AlarmRoutineConfig?> fetchConfig() async {
    final result = await _apiClient.get<Map<String, dynamic>>(
      '/alarm/routine',
      (json) => json,
    );
    final body = result.dataOrNull;
    if (body == null) return null;
    return AlarmRoutineConfig.fromJson(body);
  }

  /// Persist the config. Returns the stored config, or null on failure so the
  /// editor can keep its local state and tell the user the save did not land.
  Future<AlarmRoutineConfig?> saveConfig(AlarmRoutineConfig config) async {
    final result = await _apiClient.put<Map<String, dynamic>>(
      '/alarm/routine',
      config.toJson(),
      (json) => json,
    );
    final body = result.dataOrNull;
    if (body == null) return null;
    return AlarmRoutineConfig.fromJson(body);
  }

  /// Ask for approximate location, in context, when the weather toggle is
  /// first enabled. Returns whether a usable grant exists afterwards. Never
  /// opens system settings itself: a deniedForever answer is just "regional
  /// weather from now on".
  Future<bool> ensureLocationPermission() async {
    try {
      var permission = await Geolocator.checkPermission();
      if (permission == LocationPermission.denied) {
        permission = await Geolocator.requestPermission();
      }
      return permission == LocationPermission.always ||
          permission == LocationPermission.whileInUse;
    } catch (e) {
      AppLogger.warning('location permission check failed: $e', tag: _tag);
      return false;
    }
  }

  /// The morning brief text, or null when there is nothing to show. One
  /// request, no retry: a brief that arrives mid-conversation is worse than
  /// none, and the chat this rides on is already open and usable.
  Future<String?> fetchMorningBrief() async {
    final position = await _currentPositionOrNull();
    final query = position == null
        ? ''
        : '?lat=${position.latitude.toStringAsFixed(2)}'
              '&lon=${position.longitude.toStringAsFixed(2)}';
    try {
      final result = await _apiClient
          .get<Map<String, dynamic>>('/alarm/morning-brief$query', (json) => json)
          .timeout(_briefTimeout);
      final body = result.dataOrNull;
      final text = body?['text'];
      if (text is! String) return null;
      final trimmed = text.trim();
      return trimmed.isEmpty ? null : trimmed;
    } catch (e) {
      AppLogger.warning('morning brief fetch failed: $e', tag: _tag);
      return null;
    }
  }

  /// A quick approximate fix, or null. Last known first because the user just
  /// woke up at home: a cached position from last night is exactly right, and
  /// a cold GPS fix is not worth delaying the brief for.
  Future<Position?> _currentPositionOrNull() async {
    try {
      final permission = await Geolocator.checkPermission();
      if (permission != LocationPermission.always &&
          permission != LocationPermission.whileInUse) {
        return null;
      }
      final lastKnown = await Geolocator.getLastKnownPosition();
      if (lastKnown != null) return lastKnown;
      return await Geolocator.getCurrentPosition(
        locationSettings: const LocationSettings(
          accuracy: LocationAccuracy.low,
          timeLimit: _locationTimeout,
        ),
      );
    } catch (e) {
      AppLogger.info('no location for morning brief: $e', tag: _tag);
      return null;
    }
  }
}

class AlarmRoutineConfig {
  const AlarmRoutineConfig({
    required this.enabled,
    required this.showWeather,
    required this.actions,
  });

  final bool enabled;
  final bool showWeather;

  /// Ordered action type slugs; order here is the order in the brief.
  final List<String> actions;

  static const AlarmRoutineConfig defaults = AlarmRoutineConfig(
    enabled: false,
    showWeather: false,
    actions: AlarmRoutineService.defaultActions,
  );

  factory AlarmRoutineConfig.fromJson(Map<String, dynamic> json) {
    final rawActions = json['actions'];
    final actions = <String>[
      if (rawActions is List)
        for (final entry in rawActions)
          if (entry is Map && entry['type'] is String) entry['type'] as String,
    ];
    return AlarmRoutineConfig(
      enabled: json['enabled'] == true,
      showWeather: json['show_weather'] == true,
      actions: actions.isEmpty
          ? AlarmRoutineService.defaultActions
          : List.unmodifiable(actions),
    );
  }

  Map<String, dynamic> toJson() => {
    'enabled': enabled,
    'show_weather': showWeather,
    'actions': [
      for (final type in actions) {'type': type},
    ],
  };

  AlarmRoutineConfig copyWith({
    bool? enabled,
    bool? showWeather,
    List<String>? actions,
  }) => AlarmRoutineConfig(
    enabled: enabled ?? this.enabled,
    showWeather: showWeather ?? this.showWeather,
    actions: actions ?? this.actions,
  );
}
