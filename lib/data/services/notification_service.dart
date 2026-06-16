import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:firebase_core/firebase_core.dart';
import 'package:firebase_messaging/firebase_messaging.dart';
import 'package:flutter/foundation.dart';
import 'package:flutter_local_notifications/flutter_local_notifications.dart';

import '../../core/analytics/funnel_events.dart';
import '../../core/logging/app_logger.dart';
import '../../core/network/api_client.dart';
import 'backend_api_service.dart';
import 'posthog_analytics_service.dart';
import 'thread_notification_handler.dart';

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

/// Payload emitted when the user taps a signal-engine content notification.
/// Carries the funnel ids so the chat surface can attribute the resulting
/// session + first reply back to the originating notification.
class SignalNotificationTapPayload {
  final String notificationId;
  final String contentId;
  final String category;
  final String openingChatMessage;
  // "read" opens the source url in an in-app browser; "discuss" (or empty) opens
  // chat. url is the source article when contentKind is "read".
  final String contentKind;
  final String url;

  const SignalNotificationTapPayload({
    required this.notificationId,
    required this.contentId,
    required this.category,
    required this.openingChatMessage,
    this.contentKind = '',
    this.url = '',
  });
}

/// Payload emitted when the user taps a curiosity follow-up notification (or its
/// body on iOS, where the suggestion chips render in-chat instead of on the
/// notification). Carries the question + suggested replies so the chat surface
/// can seed Buddy's opener and the tappable pills.
class ThreadFollowUpTapPayload {
  final String threadId;
  final String question;
  final List<String> suggestedReplies;

  const ThreadFollowUpTapPayload({
    required this.threadId,
    required this.question,
    required this.suggestedReplies,
  });
}

/// Payload emitted when the user taps an icebreaker notification. An icebreaker
/// always opens chat seeded with Buddy's opener (there is no read/url branch).
/// Carries the funnel id so the chat surface can attribute the session + reply.
class IcebreakerTapPayload {
  final String notificationId;
  final String openingChatMessage;

  const IcebreakerTapPayload({
    required this.notificationId,
    required this.openingChatMessage,
  });
}

/// Payload emitted when the user taps a daily-briefing notification. The briefing
/// content is fetched by the briefing screen from `GET /briefing/today`, so the tap
/// only needs to open that screen; [briefingDate] rides along for reference.
class DailyBriefingTapPayload {
  final String briefingDate;

  const DailyBriefingTapPayload({this.briefingDate = ''});
}

/// Payload emitted when the user taps a topic-tracker live-update notification.
/// Opens chat seeded with Buddy's update opener; [topicKey]/[trackerId] ride along
/// so the chat surface can attribute the session back to the tracker.
class TrackerUpdateTapPayload {
  final String topicKey;
  final String trackerId;
  final String openingChatMessage;

  const TrackerUpdateTapPayload({
    required this.openingChatMessage,
    this.topicKey = '',
    this.trackerId = '',
  });
}

const _tag = 'NotificationService';

/// Android notification channel used for all Aura notifications.
/// Must match the `channel_id` sent by the backend (`aura_default`).
const _kAndroidChannelId = 'aura_default';
const _kAndroidChannelName = 'Aura Notifications';

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
  final PostHogAnalyticsService _postHogAnalyticsService;

  NotificationService({
    required ApiClient apiClient,
    BackendApiService? signalEventSink,
    required PostHogAnalyticsService postHogAnalyticsService,
  })  : _apiClient = apiClient,
        _signalEventSink = signalEventSink,
        _postHogAnalyticsService = postHogAnalyticsService;

  bool _initialized = false;
  String? _userId;
  StreamSubscription<String>? _tokenRefreshSubscription;
  StreamSubscription<RemoteMessage>? _foregroundSubscription;
  StreamSubscription<Map<String, dynamic>>? _threadBodyTapSub;

  final _localNotificationsPlugin = FlutterLocalNotificationsPlugin();

  final _engagementTapController = StreamController<EngagementTapPayload>.broadcast();
  final _agentNudgeTapController = StreamController<AgentNudgeTapPayload>.broadcast();
  final _signalNotificationTapController =
      StreamController<SignalNotificationTapPayload>.broadcast();
  final _threadFollowUpTapController =
      StreamController<ThreadFollowUpTapPayload>.broadcast();
  final _icebreakerTapController =
      StreamController<IcebreakerTapPayload>.broadcast();
  final _dailyBriefingTapController =
      StreamController<DailyBriefingTapPayload>.broadcast();
  final _trackerUpdateTapController =
      StreamController<TrackerUpdateTapPayload>.broadcast();

  // Emits when the user taps an engagement notification.
  Stream<EngagementTapPayload> get engagementTapStream => _engagementTapController.stream;

  // Emits when the user taps a scheduled agent nudge notification.
  Stream<AgentNudgeTapPayload> get agentNudgeTapStream => _agentNudgeTapController.stream;

  // Emits when the user taps a signal-engine content notification.
  Stream<SignalNotificationTapPayload> get signalNotificationTapStream =>
      _signalNotificationTapController.stream;

  // Emits when the user taps a curiosity follow-up notification (or its body on
  // iOS) — the chat surface seeds Buddy's question and renders the pills.
  Stream<ThreadFollowUpTapPayload> get threadFollowUpTapStream =>
      _threadFollowUpTapController.stream;

  // Emits when the user taps an icebreaker notification — the chat surface opens
  // seeded with Buddy's opener.
  Stream<IcebreakerTapPayload> get icebreakerTapStream =>
      _icebreakerTapController.stream;

  // Emits when the user taps a daily-briefing notification — opens the briefing
  // screen, which fetches today's briefing from the backend.
  Stream<DailyBriefingTapPayload> get dailyBriefingTapStream =>
      _dailyBriefingTapController.stream;

  // Emits when the user taps a topic-tracker live-update notification — the chat
  // surface opens seeded with Buddy's update.
  Stream<TrackerUpdateTapPayload> get trackerUpdateTapStream =>
      _trackerUpdateTapController.stream;

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
    // Register the thread-followup plugin + its background action callback so
    // inline replies are handled even when the app is terminated.
    await ensureThreadNotificationsInitialized();

    // The follow-up notification is built locally, so a BODY tap is delivered to
    // the local-notifications handler, not onMessageOpenedApp. Relay it into the
    // tap stream HomeViewModel listens to, and replay any terminated-launch tap.
    await _threadBodyTapSub?.cancel();
    _threadBodyTapSub = threadBodyTapStream.listen(_relayThreadBodyTap);
    unawaited(handleThreadNotificationColdLaunch());

    // 3. Get current token and register with backend.
    // On the iOS simulator APNS is unavailable, so getToken() throws
    // firebase_messaging/apns-token-not-set. Treat that one case as an expected
    // warning instead of letting it surface as an uncaught Crashlytics error.
    String? token;
    try {
      token = await FirebaseMessaging.instance.getToken();
    } on FirebaseException catch (e) {
      if (e.code != 'apns-token-not-set') rethrow;
      AppLogger.warning(
        'APNS token not set (expected on iOS simulator) — skipping FCM token registration',
        tag: _tag,
        metadata: {'userId': userId, 'code': e.code},
      );
    }
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
    await _threadBodyTapSub?.cancel();
    _tokenRefreshSubscription = null;
    _foregroundSubscription = null;
    _threadBodyTapSub = null;
    _userId = null;
    _initialized = false;
    await _engagementTapController.close();
    await _agentNudgeTapController.close();
    await _signalNotificationTapController.close();
    await _threadFollowUpTapController.close();
    await _icebreakerTapController.close();
    await _dailyBriefingTapController.close();
    await _trackerUpdateTapController.close();
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
    // No app-shown local notifications currently route here. The plugin is
    // initialized only so the Android FCM channel can be created.
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
    // Curiosity follow-ups are data-only (no notification block) so we render
    // the interactive chip notification ourselves, same as in the background.
    if (isThreadFollowUp(message)) {
      await showThreadFollowUpNotification(message);
      return;
    }

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
    final tapData = message.data;
    final uid = _userId;
    unawaited(_postHogAnalyticsService.trackEvent(
      FunnelEvents.notificationTapped,
      properties: {
        'notification_type':
            tapData['notification_type'] as String? ?? 'unknown',
        // Stamp the Firebase uid the server keyed the send on. On a cold launch
        // from a killed app this tap can fire from getInitialMessage() before
        // identifyUser(uid) lands, attaching the event to an anonymous PostHog
        // id; carrying the uid as a property keeps the funnel join independent
        // of identify() timing.
        FunnelEvents.propFirebaseUid: ?uid,
        // Funnel join keys — let PostHog filter signal-engine taps and join
        // them to the server's signal_notification_sent event.
        FunnelEvents.propNotificationOrigin:
            tapData[FunnelEvents.propNotificationOrigin] as String? ?? 'unknown',
        FunnelEvents.propNotificationId:
            tapData[FunnelEvents.propNotificationId] as String? ?? '',
        FunnelEvents.propContentId:
            tapData[FunnelEvents.propContentId] as String? ?? '',
        FunnelEvents.propCategory:
            tapData[FunnelEvents.propCategory] as String? ?? '',
      },
    ));
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

  /// Call when the user opens a "read" signal notification's article in the
  /// in-app browser. Does two things: nudges the user vector mildly toward the
  /// actual content (the content_opened signal event), and fires the read-path
  /// funnel terminal (`content_opened`) so a tapped-and-read notification is a
  /// measurable conversion — the read path never reaches the chat action step.
  Future<void> reportContentOpened({
    required String contentId,
    String? category,
    String? notificationId,
  }) async {
    if (contentId.isEmpty) return;
    unawaited(_postSignalEvents([
      _buildEventPayload(
        eventType: 'content_opened',
        contentId: contentId,
        category: category,
      ),
    ]));
    unawaited(_postHogAnalyticsService.trackEvent(
      FunnelEvents.contentOpened,
      properties: {
        FunnelEvents.propFirebaseUid: ?_userId,
        FunnelEvents.propNotificationOrigin: FunnelEvents.originSignalEngine,
        FunnelEvents.propNotificationId: ?notificationId,
        FunnelEvents.propContentId: contentId,
        FunnelEvents.propCategory: ?category,
      },
    ));
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
      'content_id': ?contentId,
      'category': ?category,
      'duration_ms': ?durationMs,
      'search_query_text': ?searchQueryText,
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
    } else if (notificationType == FunnelEvents.originSignalEngine) {
      // Signal-engine content notification. Open chat seeded with the framed
      // opener and carry the funnel ids so the chat surface can attribute the
      // session + first reply back to this notification.
      final notificationId =
          data[FunnelEvents.propNotificationId] as String? ?? '';
      if (notificationId.isNotEmpty) {
        _signalNotificationTapController.add(SignalNotificationTapPayload(
          notificationId: notificationId,
          contentId: data[FunnelEvents.propContentId] as String? ?? '',
          category: data[FunnelEvents.propCategory] as String? ?? '',
          openingChatMessage: data['opening_chat_message'] as String? ?? '',
          contentKind: data['content_kind'] as String? ?? '',
          url: data['url'] as String? ?? '',
        ));
      }
    } else if (notificationType == kThreadFollowUpType) {
      // Curiosity follow-up tapped on the body (or any iOS tap, where chips are
      // not on the notification): open chat seeded with the question + pills.
      final threadId = data['thread_id'] as String? ?? '';
      final question = data['question'] as String? ?? '';
      if (threadId.isNotEmpty && question.isNotEmpty) {
        _threadFollowUpTapController.add(ThreadFollowUpTapPayload(
          threadId: threadId,
          question: question,
          suggestedReplies: _decodeSuggestedRepliesData(data['suggested_replies']),
        ));
      }
    } else if (notificationType == FunnelEvents.originIcebreaker) {
      // Icebreaker opener tapped: open chat seeded with Buddy's opener, carrying
      // the funnel id so the chat surface can attribute the session + first reply.
      final notificationId =
          data[FunnelEvents.propNotificationId] as String? ?? '';
      final openingChatMessage = data['opening_chat_message'] as String? ?? '';
      if (openingChatMessage.isNotEmpty) {
        _icebreakerTapController.add(IcebreakerTapPayload(
          notificationId: notificationId,
          openingChatMessage: openingChatMessage,
        ));
      }
    } else if (notificationType == FunnelEvents.originBriefing) {
      _dailyBriefingTapController.add(DailyBriefingTapPayload(
        briefingDate: data['briefing_date'] as String? ?? '',
      ));
    } else if (notificationType == 'tracker_update') {
      // Topic-tracker live update tapped: open chat seeded with Buddy's update.
      final openingChatMessage = data['opening_chat_message'] as String? ?? '';
      if (openingChatMessage.isNotEmpty) {
        _trackerUpdateTapController.add(TrackerUpdateTapPayload(
          openingChatMessage: openingChatMessage,
          topicKey: data['topic_key'] as String? ?? '',
          trackerId: data['tracker_id'] as String? ?? '',
        ));
      }
    }
  }

  /// Relays an Android notification body tap (from the local-notifications
  /// handler) into [threadFollowUpTapStream], and fires the funnel tap step that
  /// the FCM `onMessageOpenedApp` path fires for every other notification.
  void _relayThreadBodyTap(Map<String, dynamic> data) {
    final threadId = data['thread_id'] as String? ?? '';
    if (threadId.isEmpty) return;

    unawaited(_postHogAnalyticsService.trackEvent(
      FunnelEvents.notificationTapped,
      properties: {
        'notification_type': kThreadFollowUpType,
        FunnelEvents.propFirebaseUid: ?_userId,
        FunnelEvents.propNotificationOrigin: FunnelEvents.originThreadEngine,
        FunnelEvents.propThreadId: threadId,
      },
    ));

    _threadFollowUpTapController.add(ThreadFollowUpTapPayload(
      threadId: threadId,
      question: data['question'] as String? ?? '',
      suggestedReplies:
          (data['suggested_replies'] as List?)?.cast<String>() ?? const [],
    ));
  }

  /// Decode the JSON-encoded `suggested_replies` string from an FCM data
  /// payload into a list. Returns an empty list on anything malformed.
  static List<String> _decodeSuggestedRepliesData(Object? raw) {
    if (raw is! String || raw.isEmpty) return const [];
    try {
      final decoded = jsonDecode(raw);
      if (decoded is List) return decoded.map((e) => e.toString()).toList();
    } catch (_) {
      // Malformed payload just means no pills — never throw out of a tap.
    }
    return const [];
  }

  /// Convenience accessor used for testing / debug screens.
  Future<String?> getToken() => FirebaseMessaging.instance.getToken();
}
