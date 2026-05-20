import 'dart:async';
import 'dart:io';

import 'package:firebase_messaging/firebase_messaging.dart';
import 'package:flutter/foundation.dart';
import 'package:flutter_local_notifications/flutter_local_notifications.dart';

import '../../core/logging/app_logger.dart';
import '../../core/network/api_client.dart';
import 'backend_api_service.dart';

/// Payload emitted when the user taps a nutrition scan-ready local notification.
class NutritionScanReadyTapPayload {
  const NutritionScanReadyTapPayload();
}

/// Payload emitted when the user taps an engagement notification.
class EngagementTapPayload {
  final String engagementId;
  final String initialMessage;
  final String agentContext;

  const EngagementTapPayload({
    required this.engagementId,
    required this.initialMessage,
    required this.agentContext,
  });
}

/// Payload emitted when the user taps a scheduled agent nudge notification.
class AgentNudgeTapPayload {
  final String agentId;
  final String chatOpener;

  const AgentNudgeTapPayload({
    required this.agentId,
    required this.chatOpener,
  });
}

const _tag = 'NotificationService';

/// Android notification channel used for all Aura notifications.
/// Must match the `channel_id` sent by the backend (`aura_default`).
const _kAndroidChannelId = 'aura_default';
const _kAndroidChannelName = 'Aura Notifications';

/// Stable notification ID for nutrition scan local notifications.
/// Must not collide with other local notification IDs.
const _kNutritionScanNotificationId = 200;

/// Centralized FCM notification service.
///
/// Call [initialize] once after the user authenticates.  It:
/// 1. Requests OS notification permission (iOS 14+ / Android 13+).
/// 2. Retrieves the FCM token and registers it with the backend.
/// 3. Listens for token refreshes and re-registers automatically.
/// 4. Handles foreground messages (shows a local system notification).
/// 5. Handles background -> foreground tap navigation.
/// 6. Creates the Android notification channel on first launch.
///
/// The service is idempotent so calling [initialize] more than once is safe.
class NotificationService {
  final ApiClient _apiClient;
  final BackendApiService? _signalEventSink;

  NotificationService({
    required ApiClient apiClient,
    BackendApiService? signalEventSink,
  })  : _apiClient = apiClient,
        _signalEventSink = signalEventSink;

  bool _initialized = false;
  String? _userId;
  StreamSubscription<String>? _tokenRefreshSubscription;
  StreamSubscription<RemoteMessage>? _foregroundSubscription;

  final _localNotificationsPlugin = FlutterLocalNotificationsPlugin();

  final _engagementTapController = StreamController<EngagementTapPayload>.broadcast();
  final _agentNudgeTapController = StreamController<AgentNudgeTapPayload>.broadcast();
  final _nutritionScanReadyTapController = StreamController<NutritionScanReadyTapPayload>.broadcast();

  // Emits when the user taps an engagement notification.
  Stream<EngagementTapPayload> get engagementTapStream => _engagementTapController.stream;

  // Emits when the user taps a scheduled agent nudge notification.
  Stream<AgentNudgeTapPayload> get agentNudgeTapStream => _agentNudgeTapController.stream;

  // Emits when the user taps the nutrition scan-ready local notification.
  Stream<NutritionScanReadyTapPayload> get nutritionScanReadyTapStream => _nutritionScanReadyTapController.stream;

  // Public API

  /// Initialize FCM for the signed-in [userId].
  /// Safe to call multiple times; subsequent calls update the stored [userId] in case the account changed.
  Future<void> initialize(String userId) async {
    _userId = userId;

    if (_initialized) {
      // Already running, so just ensure the current token is registered in
      // case the user signed in with a different account.
      final token = await FirebaseMessaging.instance.getToken();
      if (token != null) unawaited(_registerToken(token));
      return;
    }
    _initialized = true;

    // 1. Request OS permission (required for iOS 14+ and Android 13+)
    final settings = await FirebaseMessaging.instance.requestPermission(
      alert: true,
      badge: true,
      sound: true,
      provisional: false,
    );

    if (settings.authorizationStatus == AuthorizationStatus.denied) {
      AppLogger.warning(
        'Notification permission denied — FCM will not deliver alerts',
        tag: _tag,
        metadata: {'userId': userId},
      );
      return;
    }

    AppLogger.info(
      'Notification permission granted',
      tag: _tag,
      metadata: {
        'status': settings.authorizationStatus.name,
        'userId': userId,
      },
    );

    // 2. Initialize local notifications plugin + create Android channel
    await _initializeLocalNotificationsPlugin();
    await _createAndroidChannel();

    // 3. Get current token and register with backend
    final token = await FirebaseMessaging.instance.getToken();
    AppLogger.info(
      'FCM token retrieved',
      tag: _tag,
      metadata: {'tokenPreview': token?.substring(0, 20)},
    );
    if (token != null) unawaited(_registerToken(token));

    // 4. Auto-register on token refresh
    await _tokenRefreshSubscription?.cancel();
    _tokenRefreshSubscription = FirebaseMessaging.instance.onTokenRefresh
        .listen((newToken) {
      AppLogger.info(
        'FCM token refreshed — re-registering',
        tag: _tag,
        metadata: {'tokenPreview': newToken.substring(0, 20)},
      );
      unawaited(_registerToken(newToken));
    });

    // 5. Foreground messages → show local notification
    await _foregroundSubscription?.cancel();
    _foregroundSubscription = FirebaseMessaging.onMessage.listen(
      _handleForegroundMessage,
    );

    // 6. App opened from background via notification tap
    FirebaseMessaging.onMessageOpenedApp.listen(_handleNotificationTap);

    // 7. App opened from terminated state via notification tap
    final initialMessage = await FirebaseMessaging.instance.getInitialMessage();
    if (initialMessage != null) {
      _handleNotificationTap(initialMessage);
    }
  }

  /// Call on sign-out to clean up listeners.
  Future<void> dispose() async {
    await _tokenRefreshSubscription?.cancel();
    await _foregroundSubscription?.cancel();
    _tokenRefreshSubscription = null;
    _foregroundSubscription = null;
    _userId = null;
    _initialized = false;
    await _engagementTapController.close();
    await _agentNudgeTapController.close();
    await _nutritionScanReadyTapController.close();
  }

  // Public local notification API

  /// Show an immediate local system notification for nutrition scan results.
  ///
  /// Used when the user has backgrounded the app during a scan and 
  /// the result arrives or the Q&A is awaiting their input.
  Future<void> showNutritionScanLocalNotification({
    required String title,
    required String body,
  }) async {
    const androidDetails = AndroidNotificationDetails(
      _kAndroidChannelId,
      _kAndroidChannelName,
      importance: Importance.high,
      priority: Priority.high,
      playSound: true,
      icon: '@drawable/ic_notification',
    );
    const iosDetails = DarwinNotificationDetails();
    const details = NotificationDetails(android: androidDetails, iOS: iosDetails);

    try {
      await _localNotificationsPlugin.show(
        id: _kNutritionScanNotificationId,
        title: title,
        body: body,
        notificationDetails: details,
        payload: 'nutrition_scan_ready',
      );
    } catch (e) {
      AppLogger.error(
        'Failed to show nutrition scan local notification',
        error: e,
        tag: _tag,
      );
    }
  }

  // Private helpers 

  Future<void> _initializeLocalNotificationsPlugin() async {
    const initSettingsAndroid = AndroidInitializationSettings('@drawable/ic_notification');
    const initSettingsIOS = DarwinInitializationSettings();
    const initSettings = InitializationSettings(
      android: initSettingsAndroid,
      iOS: initSettingsIOS,
    );

    await _localNotificationsPlugin.initialize(
      settings: initSettings,
      onDidReceiveNotificationResponse: _handleLocalNotificationTap,
    );

    AppLogger.debug(
      'Local notifications plugin initialized',
      tag: _tag,
    );
  }

  void _handleLocalNotificationTap(NotificationResponse response) {
    if (response.payload == 'nutrition_scan_ready') {
      _nutritionScanReadyTapController.add(const NutritionScanReadyTapPayload());
    }
  }

  Future<void> _registerToken(String token) async {
    final uid = _userId;
    if (uid == null) return;

    final platform = Platform.isIOS
        ? 'ios'
        : Platform.isAndroid
            ? 'android'
            : 'web';

    final result = await _apiClient.post(
      '/devices/register',
      {'token': token, 'platform': platform},
      (json) => json,
    );

    result.when(
      success: (_) => AppLogger.info(
        'FCM token registered with backend',
        tag: _tag,
        metadata: {'platform': platform, 'tokenPreview': token.substring(0, 20)},
      ),
      failure: (error) => AppLogger.error(
        'Failed to register FCM token',
        error: error,
        tag: _tag,
      ),
    );
  }

  Future<void> _createAndroidChannel() async {
    const channel = AndroidNotificationChannel(
      _kAndroidChannelId,
      _kAndroidChannelName,
      importance: Importance.high,
      enableVibration: true,
      playSound: true,
    );
    await _localNotificationsPlugin
        .resolvePlatformSpecificImplementation<
            AndroidFlutterLocalNotificationsPlugin>()
        ?.createNotificationChannel(channel);
    AppLogger.debug(
      'Android notification channel created',
      tag: _tag,
      metadata: {'channelId': _kAndroidChannelId},
    );
  }

  /// Show a system notification while the app is in the foreground.
  Future<void> _handleForegroundMessage(RemoteMessage message) async {
    final notification = message.notification;
    if (notification == null) return;

    AppLogger.info(
      'FCM foreground message received',
      tag: _tag,
      metadata: {
        'messageId': message.messageId,
        'title': notification.title,
        'notificationType': message.data['notification_type'],
      },
    );

    // Let FCM render the native OS banner even while the app is foregrounded.
    await FirebaseMessaging.instance.setForegroundNotificationPresentationOptions(
      alert: true,
      badge: true,
      sound: true,
    );
  }

  /// Handle notification tap (from background or terminated state).
  void _handleNotificationTap(RemoteMessage message) {
    AppLogger.info(
      'Notification tapped',
      tag: _tag,
      metadata: {
        'messageId': message.messageId,
        'notificationType': message.data['notification_type'],
        'reminderId': message.data['reminder_id'],
      },
    );
    _reportNotificationOpened(message.data);
    dispatchNotificationTap(message.data);
  }

  /// Public hook the chat/feed surfaces can call when the user dismisses a
  /// notification-originated chat thread without engaging with the content.
  Future<void> reportContentSkipped({
    required String contentId,
    String? category,
  }) async {
    await _postSignalEvents([
      _buildEventPayload(
        eventType: 'content_skipped',
        contentId: contentId,
        category: category,
      ),
    ]);
  }

  /// Call once per cold app open. Records the local-time slot so the engine
  /// learns when the user reaches for the app organically.
  Future<void> reportAppOpen() async {
    await _postSignalEvents([
      _buildEventPayload(eventType: 'app_open'),
    ]);
  }

  void _reportNotificationOpened(Map<String, dynamic> data) {
    if ((data['notification_origin'] as String?) != 'signal_engine') return;
    final contentId = data['content_id'] as String?;
    if (contentId == null || contentId.isEmpty) return;
    final notificationId = data['notification_id'] as String? ?? contentId;
    unawaited(_postSignalEvents([
      _buildEventPayload(
        eventType: 'notification_opened',
        // Outcome rows are keyed on the notification_id; the backend expects
        // it in content_id so resolve_outcome can find the right row.
        contentId: notificationId,
        category: data['category'] as String?,
      ),
    ]));
  }

  Map<String, dynamic> _buildEventPayload({
    required String eventType,
    String? contentId,
    String? category,
    int? durationMs,
    String? searchQueryText,
  }) {
    final now = DateTime.now();
    return {
      'event_type': eventType,
      if (contentId != null) 'content_id': contentId,
      if (category != null) 'category': category,
      if (durationMs != null) 'duration_ms': durationMs,
      if (searchQueryText != null) 'search_query_text': searchQueryText,
      'user_local_hour': now.hour,
      'user_local_minute': now.minute,
    };
  }

  Future<void> _postSignalEvents(List<Map<String, dynamic>> events) async {
    final sink = _signalEventSink;
    if (sink == null || events.isEmpty) return;
    try {
      await sink.postSignalEvents(events);
    } catch (e) {
      AppLogger.warning(
        'Failed to post signal events',
        tag: _tag,
        metadata: {'eventCount': events.length, 'error': e.toString()},
      );
    }
  }

  /// Routes FCM data payloads to the correct tap stream.
  ///
  /// Extracted for testability, production code calls [_handleNotificationTap]
  /// which delegates here after logging.
  @visibleForTesting
  void dispatchNotificationTap(Map<String, dynamic> data) {
    final notificationType = data['notification_type'] as String?;

    if (notificationType == 'engagement') {
      final engagementId = data['engagement_id'] as String? ?? '';
      final initialMessage = data['initial_message'] as String? ?? '';
      final agentContext = data['agent_context'] as String? ?? '';

      if (engagementId.isNotEmpty && initialMessage.isNotEmpty) {
        _engagementTapController.add(EngagementTapPayload(
          engagementId: engagementId,
          initialMessage: initialMessage,
          agentContext: agentContext,
        ));
      }
    } else if (notificationType == 'agent_nudge') {
      final agentId = data['agent_id'] as String? ?? '';
      final chatOpener = data['opening_chat_message'] as String? ?? '';

      if (agentId.isNotEmpty) {
        _agentNudgeTapController.add(AgentNudgeTapPayload(
          agentId: agentId,
          chatOpener: chatOpener,
        ));
      }
    } else if (notificationType == 'daily_nudge' ||
        notificationType == 'meeting_reminder') {
      final initialMessage = data['initial_message'] as String? ?? '';
      if (initialMessage.isNotEmpty) {
        _engagementTapController.add(EngagementTapPayload(
          engagementId: '',
          initialMessage: initialMessage,
          agentContext: '',
        ));
      }
    }
  }

  /// Convenience accessor used for testing / debug screens.
  Future<String?> getToken() => FirebaseMessaging.instance.getToken();
}
