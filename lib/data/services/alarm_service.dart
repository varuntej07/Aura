import 'dart:convert';
import 'dart:io';

import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';
import 'package:flutter_local_notifications/flutter_local_notifications.dart';

import '../../core/network/api_client.dart';
import '../../core/logging/app_logger.dart';
import 'alarm_voice_cache.dart';

/// The Dart half of the alarm tier.
///
/// The division of labour is the whole design, so it is worth stating plainly:
/// **Kotlin owns the schedule, Dart owns the network.**
///
/// An alarm rings from a schedule the OS holds locally, registered through
/// `AlarmManager.setAlarmClock`. That has to be true because FCM cannot wake a
/// doze'd phone at 3 AM, no priority flag changes it, and because an alarm has
/// to work in airplane mode. When it fires there may be no Flutter engine alive
/// at all, so nothing on this side can be on the ringing path.
///
/// What this side does instead:
///   * fetches the authoritative schedule (`GET /reminders/alarms`) and hands it
///     down to be reconciled, because only Dart holds a live Firebase token;
///   * acts on the silent `alarm_sync` control pushes so a new alarm is armed
///     immediately rather than at the next reconcile;
///   * flushes the acks Kotlin queued while the app was not running, since
///     Dismiss and Snooze never open the app;
///   * reports whether the OS will actually let Buddy ring, so Buddy can promise
///     a nudge instead of a wake-up when it will not.
///
/// Every method here fails soft. A failure means the schedule is stale, not that
/// an alarm is lost: whatever is already armed still rings, and the next
/// reconcile repairs the rest.
class AlarmService {
  AlarmService({required ApiClient apiClient})
      : _apiClient = apiClient,
        _voiceCache = AlarmVoiceCache(apiClient: apiClient);

  /// For the FCM background isolate, which has no DI graph and no ApiClient.
  ///
  /// Only the MethodChannel half works here, which is exactly the half that
  /// matters in the background: arming the local schedule the instant a control
  /// push lands. Every network method degrades to a no-op rather than throwing,
  /// because the app will reconcile properly the next time it runs.
  AlarmService.forBackgroundIsolate()
      : _apiClient = null,
        _voiceCache = null;

  final ApiClient? _apiClient;

  /// Null in the background isolate, which has no ApiClient to fetch with. A
  /// control push handled there arms with no spoken line and the next reconcile
  /// fills it in; the alarm itself is armed either way.
  final AlarmVoiceCache? _voiceCache;

  static const MethodChannel _channel = MethodChannel('dev.varuntej.aura/alarm');

  /// notification_type on the silent control pushes from `services/alarm_sync.py`.
  static const String controlMessageType = 'alarm_sync';

  static const String _tag = 'AlarmService';

  /// Must match CHANNEL_ID in AlarmService.kt. A channel's importance and sound
  /// are immutable after creation, so the two sides cannot be allowed to drift.
  static const String alarmChannelId = 'aura_alarm';
  static const String alarmChannelName = 'Alarms';

  final FlutterLocalNotificationsPlugin _localPlugin = FlutterLocalNotificationsPlugin();
  bool _localPluginReady = false;

  /// Android is the only platform that can hold a real alarm schedule today.
  /// iOS has no equivalent to `setAlarmClock` without Apple's critical-alerts
  /// entitlement, so the channel is simply absent there and every call no-ops
  /// rather than throwing MissingPluginException on a hot path.
  static bool get isSupported => !kIsWeb && Platform.isAndroid;

  AlarmCapabilities _capabilities = const AlarmCapabilities.unsupported();
  AlarmCapabilities get capabilities => _capabilities;

  /// Whether this device will actually ring.
  ///
  /// Read BEFORE Buddy confirms an alarm. Android 14 denies exact alarms by
  /// default and denies them silently, `setAlarmClock` degrades and nothing
  /// tells the user their 3 AM alarm is never going to fire. Confirming a
  /// wake-up that cannot happen is the original bug wearing a new coat.
  Future<AlarmCapabilities> refreshCapabilities() async {
    if (!isSupported) return _capabilities = const AlarmCapabilities.unsupported();
    try {
      final raw = await _channel.invokeMapMethod<String, dynamic>('capabilities');
      _capabilities = AlarmCapabilities.fromMap(raw ?? const {});
    } catch (e) {
      AppLogger.warning('capability check failed: $e', tag: _tag);
      _capabilities = const AlarmCapabilities.unsupported();
    }
    return _capabilities;
  }

  /// Whether the ability to ring changed since it was last reported to the
  /// backend, which is the signal to re-register and re-arm.
  ///
  /// The grant happens in a system Settings screen, so the app finds out by
  /// noticing on resume. Nothing tells it directly.
  bool get canRingChangedSinceReport => _lastReportedCanRing != _capabilities.canRing;

  bool? _lastReportedCanRing;

  /// The value to send with device registration, and the record that it was sent.
  bool markCanRingReported() {
    _lastReportedCanRing = _capabilities.canRing;
    return _capabilities.canRing;
  }

  /// Re-register every stored alarm, upgrading any that were armed inexactly
  /// into real alarms. Called when the permission is newly granted.
  Future<int> rearmAll() async {
    if (!isSupported) return 0;
    try {
      return await _channel.invokeMethod<int>('rearmAll') ?? 0;
    } catch (e) {
      AppLogger.warning('rearmAll failed: $e', tag: _tag);
      return 0;
    }
  }

  /// Send the user to the system page where exact alarms are granted. There is
  /// no runtime dialog for this permission, only a Settings screen.
  Future<bool> requestExactAlarmAccess() => _invokeBool('requestExactAlarmAccess');

  Future<bool> requestFullScreenIntentAccess() =>
      _invokeBool('requestFullScreenIntentAccess');

  /// Pull the complete armed set from the server and make the device match it.
  ///
  /// Called on app start, on resume, and after the app is reinstalled. This is
  /// the path that repairs every failure of the push path: a control message
  /// dropped while the phone was off, a reboot that wiped the OS alarm table, a
  /// device that signed in after the alarm was created.
  ///
  /// Deliberately does nothing on a failed fetch. The server's answer is
  /// COMPLETE, so an empty list means "disarm everything", applying that after
  /// a network error or a 503 would cancel every alarm the user has.
  Future<int> reconcile() async {
    final api = _apiClient;
    if (!isSupported || api == null) return 0;
    final result = await api.get<Map<String, dynamic>>(
      '/reminders/alarms',
      (json) => json,
    );
    final body = result.dataOrNull;
    if (body == null) {
      AppLogger.warning('alarm sync fetch failed; keeping the current schedule', tag: _tag);
      return 0;
    }
    final alarms = body['alarms'];
    if (alarms is! List) {
      AppLogger.warning('alarm sync returned no list; keeping the current schedule', tag: _tag);
      return 0;
    }
    final rows = await _withVoiceClips(alarms);

    try {
      final armed = await _channel.invokeMethod<int>('reconcile', {
        'alarms': jsonEncode(rows),
      });
      AppLogger.info('reconciled ${rows.length} alarms, $armed armed', tag: _tag);
      return armed ?? 0;
    } catch (e) {
      AppLogger.warning('reconcile failed: $e', tag: _tag);
      return 0;
    }
  }

  /// Attach a cached spoken wake-up line to every alarm that asked for one.
  ///
  /// Runs before the schedule is handed to Kotlin, because arm time is the only
  /// moment this side is guaranteed to be alive with a network. Anything that
  /// cannot be fetched is simply left off: the alarm arms regardless and rings
  /// with its tone looping uninterrupted.
  ///
  /// Also the one place that sees the COMPLETE armed set, so it is where stale
  /// clips are evicted.
  Future<List<Map<String, dynamic>>> _withVoiceClips(List<dynamic> alarms) async {
    final rows = <Map<String, dynamic>>[];
    final tags = <String, String>{};

    for (final alarm in alarms) {
      if (alarm is! Map) continue;
      final row = Map<String, dynamic>.from(alarm);
      final reminderId = (row['reminder_id'] ?? '').toString();
      final clipTag = (row['clip_tag'] ?? '').toString();
      if (reminderId.isEmpty) continue;

      final cache = _voiceCache;
      if (cache != null && clipTag.isNotEmpty) {
        tags[reminderId] = clipTag;
        final path = await cache.ensureClip(reminderId: reminderId, clipTag: clipTag);
        if (path != null) row['voice_clip_path'] = path;
      }
      rows.add(row);
    }

    // Deliberately after the loop and given every armed id, including the ones
    // whose fetch just failed: eviction is decided by what SHOULD be held, not
    // by what happened to succeed this pass.
    await _voiceCache?.retainOnly(tags);
    return rows;
  }

  /// Act on a silent `alarm_sync` control push.
  ///
  /// Returns true when the message was one of ours and was handled, so callers
  /// can stop processing it as a notification, it has no user-visible form.
  Future<bool> handleControlMessage(Map<String, dynamic> data) async {
    if (data['notification_type'] != controlMessageType) return false;
    if (!isSupported) return true;

    final op = (data['op'] ?? '').toString();
    final reminderId = (data['reminder_id'] ?? '').toString();
    if (reminderId.isEmpty) return true;

    try {
      switch (op) {
        case 'schedule':
          final clipTag = (data['clip_tag'] ?? '').toString();
          // Best effort, and null in the background isolate where there is no
          // ApiClient at all. Never blocks the arm: a schedule that reached the
          // device is worth registering with the OS immediately even if the
          // spoken line has to wait for the next reconcile.
          final clipPath = clipTag.isEmpty
              ? null
              : await _voiceCache?.ensureClip(
                  reminderId: reminderId,
                  clipTag: clipTag,
                );
          await _channel.invokeMethod<bool>('arm', {
            'alarm': jsonEncode({
              'reminder_id': reminderId,
              'message': data['message'] ?? '',
              'trigger_at': data['trigger_at'] ?? '',
              'local_time': data['local_time'] ?? '',
              'timezone': data['timezone'] ?? '',
              'snooze_count': 0,
              'tone': data['tone'] ?? '',
              if (clipPath != null) 'voice_clip_path': clipPath,
            }),
          });
          // A re-arm for an alarm ringing right now means "stop and take this
          // new time", the snooze case, where another device acked first.
          await _channel.invokeMethod<bool>('stopRinging');
        case 'cancel':
          await _channel.invokeMethod<bool>('disarm', {'reminder_id': reminderId});
        case 'stop':
          // The user already dealt with this alarm on another device.
          await _channel.invokeMethod<bool>('disarm', {'reminder_id': reminderId});
          await _channel.invokeMethod<bool>('stopRinging');
        default:
          AppLogger.warning('unknown alarm control op: $op', tag: _tag);
      }
    } catch (e) {
      AppLogger.warning('alarm control message failed ($op): $e', tag: _tag);
    }
    return true;
  }

  /// Render the server's backstop for an alarm, unless this device already rang.
  ///
  /// Returns true when the message was an alarm backstop and has been dealt
  /// with, either by rendering it or by deliberately swallowing it.
  ///
  /// The backstop is sent data-only precisely so this decision can be made here.
  /// An OS-drawn banner arrives with no chance to ask whether the local alarm
  /// already fired, and a second alert seconds after the user silenced the first
  /// one is its own bug. When the local schedule never existed (a device that
  /// signed in after the alarm was set, a reboot before the first reconcile),
  /// this is the only thing that fires at all, so it is rendered on the alarm
  /// channel with a full-screen intent rather than as a quiet banner.
  Future<bool> handleFallback(Map<String, dynamic> data) async {
    if ((data['alarm_fallback'] ?? '').toString() != '1') return false;
    if (await shouldSuppressFallback(data)) {
      AppLogger.info('alarm backstop suppressed, the local alarm already rang', tag: _tag);
      return true;
    }
    if (!isSupported) return false;

    try {
      await _ensureLocalPlugin();
      final body = (data['alarm_body'] ?? '').toString();
      await _localPlugin.show(
        id: (data['reminder_id'] ?? '').toString().hashCode.abs() % 0x7FFFFFFF,
        title: 'Buddy',
        body: body.isEmpty ? 'Time to get up.' : body,
        notificationDetails: const NotificationDetails(
          android: AndroidNotificationDetails(
            alarmChannelId,
            alarmChannelName,
            importance: Importance.max,
            priority: Priority.max,
            category: AndroidNotificationCategory.alarm,
            audioAttributesUsage: AudioAttributesUsage.alarm,
            fullScreenIntent: true,
            ongoing: true,
            autoCancel: false,
          ),
        ),
        payload: jsonEncode({'data': data}),
      );
    } catch (e) {
      AppLogger.warning('alarm backstop render failed: $e', tag: _tag);
    }
    return true;
  }

  Future<void> _ensureLocalPlugin() async {
    if (_localPluginReady) return;
    await _localPlugin.initialize(
      settings: const InitializationSettings(
        android: AndroidInitializationSettings('@drawable/ic_notification'),
        iOS: DarwinInitializationSettings(),
      ),
    );
    // Matches the channel AlarmService.kt creates natively. Creating it from
    // both sides is safe (the OS ignores a repeat) and necessary: whichever path
    // runs first on a fresh install must not post to a channel that does not
    // exist yet, or the notification is dropped without a trace.
    await _localPlugin
        .resolvePlatformSpecificImplementation<AndroidFlutterLocalNotificationsPlugin>()
        ?.createNotificationChannel(
          const AndroidNotificationChannel(
            alarmChannelId,
            alarmChannelName,
            description: 'Alarms Buddy sets when you ask to be woken up.',
            importance: Importance.max,
            playSound: true,
            enableVibration: true,
          ),
        );
    _localPluginReady = true;
  }

  /// Whether the server's backstop push for this alarm should be swallowed.
  ///
  /// The scheduler pushes a banner at the alarm's moment as cover for a device
  /// that never got a local schedule. When the local alarm DID ring, that banner
  /// arrives seconds later and would wake a half-asleep user a second time for
  /// the thing that already worked.
  Future<bool> shouldSuppressFallback(Map<String, dynamic> data) async {
    if (!isSupported) return false;
    if ((data['alarm_fallback'] ?? '').toString() != '1') return false;
    final reminderId = (data['reminder_id'] ?? '').toString();
    if (reminderId.isEmpty) return false;
    try {
      return await _channel.invokeMethod<bool>(
            'firedRecently',
            {'reminder_id': reminderId},
          ) ??
          false;
    } catch (e) {
      AppLogger.warning('fallback suppression check failed: $e', tag: _tag);
      return false;
    }
  }

  /// Post the acks Kotlin took while this side was not running.
  ///
  /// Dismiss and Snooze never open the app, and at 3 AM there may be no network
  /// at all, so the device settles the alarm locally and queues the ack. The
  /// queue is cleared only after the server has accepted every entry: a partial
  /// flush that cleared anyway would silently lose the record of a snooze.
  Future<void> flushPendingAcks() async {
    if (!isSupported) return;
    final List<dynamic> queued;
    try {
      final raw = await _channel.invokeMethod<String>('pendingAcks');
      if (raw == null || raw.isEmpty) return;
      final decoded = jsonDecode(raw);
      if (decoded is! List || decoded.isEmpty) return;
      queued = decoded;
    } catch (e) {
      AppLogger.warning('could not read pending acks: $e', tag: _tag);
      return;
    }

    var allAccepted = true;
    for (final entry in queued) {
      if (entry is! Map) continue;
      final reminderId = (entry['reminder_id'] ?? '').toString();
      final action = (entry['action'] ?? '').toString();
      if (reminderId.isEmpty || action.isEmpty) continue;
      // "unanswered" is the give-up marker: the alarm rang for ten minutes and
      // nobody touched it. Recorded as a dismissal server-side, but it is worth
      // keeping the distinction locally, an ignored alarm is a product signal,
      // not a user action.
      final accepted = await _postAck(
        reminderId,
        action == 'unanswered' ? 'dismiss' : action,
        entry['next_trigger_at']?.toString(),
      );
      allAccepted = allAccepted && accepted;
    }

    if (allAccepted) {
      try {
        await _channel.invokeMethod<bool>('clearPendingAcks');
      } catch (e) {
        AppLogger.warning('could not clear pending acks: $e', tag: _tag);
      }
    }
  }

  /// Settle an alarm from the app itself (as opposed to the native full-screen
  /// UI), e.g. when "I'm up" has just opened a chat turn.
  Future<bool> acknowledge(
    String reminderId, {
    required String action,
    String? nextTriggerAt,
  }) =>
      _postAck(reminderId, action, nextTriggerAt);

  Future<bool> _postAck(
    String reminderId,
    String action,
    String? nextTriggerAt,
  ) async {
    final api = _apiClient;
    if (api == null) return false;
    final result = await api.post<Map<String, dynamic>>(
      '/reminders/$reminderId/ack',
      {
        'action': action,
        if (nextTriggerAt != null && nextTriggerAt.isNotEmpty)
          'next_trigger_at': nextTriggerAt,
      },
      (json) => json,
    );
    if (!result.isSuccess) {
      AppLogger.warning('ack failed for $reminderId ($action)', tag: _tag);
    }
    return result.isSuccess;
  }

  /// The sound the user picked from their own device, or null if none.
  ///
  /// Read from the device rather than from Firestore on purpose: the URI points
  /// into this phone's own storage, would mean nothing on another device, and
  /// can name a file the user has no reason to have told a server about. Only
  /// the `device` slug itself syncs.
  Future<DeviceTone?> deviceTone() async {
    if (!isSupported) return null;
    try {
      final raw = await _channel.invokeMapMethod<String, dynamic>('deviceTone');
      return DeviceTone.fromMap(raw ?? const {});
    } catch (e) {
      AppLogger.warning('device tone read failed: $e', tag: _tag);
      return null;
    }
  }

  /// Open the system ringtone picker, returning the resulting pick.
  ///
  /// Returns null when the user backed out without choosing, or when no picker
  /// is available. Null is not an error the caller has to explain.
  Future<DeviceTone?> pickDeviceTone() async {
    if (!isSupported) return null;
    try {
      final raw = await _channel.invokeMapMethod<String, dynamic>('pickDeviceTone');
      final tone = DeviceTone.fromMap(raw ?? const {});
      return tone.uri.isEmpty ? null : tone;
    } catch (e) {
      AppLogger.warning('device tone pick failed: $e', tag: _tag);
      return null;
    }
  }

  /// Mirror the Firestore alarm default into native SharedPreferences.
  ///
  /// Firestore remains authoritative and the server still resolves the tone
  /// carried by each schedule. The mirror is available to native/offline alarm
  /// code before a Flutter engine or network exists.
  Future<bool> mirrorDefaultTone(String tone) async {
    if (!isSupported) return false;
    try {
      return await _channel.invokeMethod<bool>(
            'setDefaultTone',
            {'tone': tone},
          ) ??
          false;
    } catch (e) {
      AppLogger.warning('default tone mirror failed: $e', tag: _tag);
      return false;
    }
  }

  Future<bool> _invokeBool(String method) async {
    if (!isSupported) return false;
    try {
      return await _channel.invokeMethod<bool>(method) ?? false;
    } catch (e) {
      AppLogger.warning('$method failed: $e', tag: _tag);
      return false;
    }
  }
}

/// A sound chosen from the phone's own storage with the system picker.
@immutable
class DeviceTone {
  const DeviceTone({required this.uri, required this.title});

  final String uri;

  /// The name as the OS reports it, resolved fresh each read so a renamed or
  /// deleted track shows the truth rather than a label kept from months ago.
  final String title;

  bool get isEmpty => uri.isEmpty;

  factory DeviceTone.fromMap(Map<String, dynamic> map) => DeviceTone(
        uri: (map['uri'] ?? '').toString(),
        title: (map['title'] ?? '').toString(),
      );
}

/// What the OS will actually permit on this device.
@immutable
class AlarmCapabilities {
  const AlarmCapabilities({
    required this.canScheduleExact,
    required this.canUseFullScreenIntent,
    this.degradedAlarmCount = 0,
  });

  const AlarmCapabilities.unsupported()
      : canScheduleExact = false,
        canUseFullScreenIntent = false,
        degradedAlarmCount = 0;

  final bool canScheduleExact;
  final bool canUseFullScreenIntent;

  /// Alarms currently armed inexactly because the permission was refused. These
  /// still fire, but can be several minutes late under Doze, so any UI showing
  /// them has to say so rather than presenting them as alarms.
  final int degradedAlarmCount;

  /// The one that decides whether Buddy may promise a wake-up. Full-screen
  /// intent is an upgrade: without it the alarm still rings at alarm volume and
  /// vibrates, it just does not take over the lock screen.
  bool get canRing => canScheduleExact;

  factory AlarmCapabilities.fromMap(Map<String, dynamic> map) => AlarmCapabilities(
        canScheduleExact: map['can_schedule_exact'] == true,
        canUseFullScreenIntent: map['can_use_full_screen_intent'] == true,
        degradedAlarmCount: (map['degraded_alarm_count'] as int?) ?? 0,
      );
}
