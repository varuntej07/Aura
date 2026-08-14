import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:firebase_core/firebase_core.dart';
import 'package:firebase_messaging/firebase_messaging.dart';
import 'package:flutter/widgets.dart';
import 'package:flutter_local_notifications/flutter_local_notifications.dart';

import '../../core/analytics/funnel_events.dart';
import '../../core/logging/app_logger.dart';
import '../../core/network/api_client.dart';
import 'backend_api_service.dart';
import '../../core/analytics/analytics_client.dart';
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
  // Buddy-facing "why I reached out" note. Sent to the chat backend on the first
  // reply only, so Buddy stays oriented on the opener it sent. Never shown to the user.
  final String notificationReason;

  const SignalNotificationTapPayload({
    required this.notificationId,
    required this.contentId,
    required this.category,
    required this.openingChatMessage,
    this.contentKind = '',
    this.url = '',
    this.notificationReason = '',
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
  final String notificationReason;

  const ThreadFollowUpTapPayload({
    required this.threadId,
    required this.question,
    required this.suggestedReplies,
    this.notificationReason = '',
  });
}

/// Payload emitted when the user taps an icebreaker notification. An icebreaker
/// always opens chat seeded with Buddy's opener (there is no read/url branch).
/// Carries the funnel id so the chat surface can attribute the session + reply.
class IcebreakerTapPayload {
  final String notificationId;
  final String openingChatMessage;
  final String notificationReason;

  const IcebreakerTapPayload({
    required this.notificationId,
    required this.openingChatMessage,
    this.notificationReason = '',
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

/// Payload emitted when the user taps a "Buddy replied" notification — the reply that
/// finished server-side after they left mid-stream. Opens the existing chat session and
/// hydrates the reply (keyed by [clientMessageId]).
class ChatReplyTapPayload {
  final String sessionId;
  final String clientMessageId;

  const ChatReplyTapPayload({
    required this.clientMessageId,
    this.sessionId = '',
  });
}

/// Payload emitted when the user taps a trial-lifecycle notification (3-days-left
/// warning or trial-ended notice, `entitlement_notifications.py`). Opens the paywall
/// with copy contextualized by [variant] ('3d_warning' | 'expired').
class TrialTapPayload {
  final String variant;

  const TrialTapPayload({this.variant = ''});
}

/// Payload emitted when an entitlement-updated push arrives (the billing
/// webhook just rewrote this account's entitlement: purchase, renewal, payment
/// trouble, refund). [tier]/[status] are hints for logging; the consumer's job
/// is to refetch GET /entitlement, never to trust the push payload itself.
class EntitlementUpdatedPayload {
  final String tier;
  final String status;

  const EntitlementUpdatedPayload({this.tier = '', this.status = ''});
}

/// notification_type sent by the backend's billing webhook sync push.
const kEntitlementUpdatedType = 'entitlement_updated';

const _tag = 'NotificationService';

/// Android notification channel used for all Aura notifications.
/// Must match the `channel_id` sent by the backend (`aura_default`).
const _kAndroidChannelId = 'aura_default';
const _kAndroidChannelName = 'Aura Notifications';
const _kTokenResumeSyncInterval = Duration(minutes: 5);

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
  final AnalyticsClient _postHogAnalyticsService;

  NotificationService({
    required ApiClient apiClient,
    BackendApiService? signalEventSink,
    required AnalyticsClient postHogAnalyticsService,
  })  : _apiClient = apiClient,
        _signalEventSink = signalEventSink,
        _postHogAnalyticsService = postHogAnalyticsService;

  bool _initialized = false;
  String? _userId;
  StreamSubscription<String>? _tokenRefreshSubscription;
  StreamSubscription<RemoteMessage>? _foregroundSubscription;
  StreamSubscription<Map<String, dynamic>>? _threadBodyTapSub;
  AppLifecycleListener? _appLifecycleListener;
  Future<void>? _tokenSyncInFlight;
  DateTime? _lastTokenSyncStartedAt;

  final _localNotificationsPlugin = FlutterLocalNotificationsPlugin();

  final _engagementTapController = StreamController<EngagementTapPayload>.broadcast();
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
  final _chatReplyTapController =
      StreamController<ChatReplyTapPayload>.broadcast();
  final _trialTapController = StreamController<TrialTapPayload>.broadcast();
  final _entitlementUpdatedController =
      StreamController<EntitlementUpdatedPayload>.broadcast();

  // Emits when the user taps an engagement notification.
  Stream<EngagementTapPayload> get engagementTapStream => _engagementTapController.stream;

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

  // Emits when the user taps a "Buddy replied" notification — opens the existing chat
  // session and hydrates the reply that finished while the app was backgrounded.
  Stream<ChatReplyTapPayload> get chatReplyTapStream =>
      _chatReplyTapController.stream;

  // Emits when the user taps a trial-lifecycle notification — opens the paywall with
  // copy contextualized to why they're there.
  Stream<TrialTapPayload> get trialTapStream => _trialTapController.stream;

  // Emits when an entitlement-updated push arrives (on receipt while
  // foregrounded, or on tap) — the subscriber refetches /entitlement so every
  // surface reflects the purchase/downgrade within seconds.
  Stream<EntitlementUpdatedPayload> get entitlementUpdatedStream =>
      _entitlementUpdatedController.stream;

  // Public API

  /// Initialize FCM for the signed-in [userId].
  /// Safe to call multiple times; subsequent calls update the stored [userId] in case the account changed.
  Future<void> initialize(String userId) async {
    _userId = userId;

    if (_initialized) {
      // Already running, so just ensure the current token is registered in
      // case the user signed in with a different account.
      await _syncCurrentToken(reason: 'authentication', force: true);
      return;
    }

    // 1. Request OS permission (required for iOS 14+ and Android 13+)
    final settings = await FirebaseMessaging.instance.requestPermission(
      alert: true,
      badge: true,
      sound: true,
      provisional: false,
    );

    if (settings.authorizationStatus == AuthorizationStatus.denied) {
      AppLogger.warning(
        'Notification permission denied, FCM will not deliver alerts',
        tag: _tag,
        metadata: {'userId': userId},
      );
      // Watch for resume anyway. initialize() is otherwise only re-driven by an
      // auth-state emission, so without this a user who flips the switch in OS
      // Settings stays unreachable until the next full app restart — and since
      // no token is registered while denied, the backend sees them as having no
      // device at all.
      _installLifecycleListener();
      return;
    }

    await _completeInitialization(userId, settings.authorizationStatus);
  }

  /// Everything that requires a granted notification permission.
  ///
  /// Split out of [initialize] so the resume path can complete initialization
  /// after a permission was granted in OS Settings, without re-prompting.
  Future<void> _completeInitialization(
    String userId,
    AuthorizationStatus status,
  ) async {
    _initialized = true;

    AppLogger.info(
      'Notification permission granted',
      tag: _tag,
      metadata: {
        'status': status.name,
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

    // 3. Register the current token before initialization completes. Recheck it
    // whenever the app resumes so a token invalidated by FCM, or a registration
    // missed while offline, heals without requiring a cold restart.
    _installLifecycleListener();
    await _syncCurrentToken(reason: 'initialization', force: true);

    // 4. Auto-register on token refresh
    await _tokenRefreshSubscription?.cancel();
    _tokenRefreshSubscription = FirebaseMessaging.instance.onTokenRefresh
        .listen((newToken) {
      AppLogger.info(
        'FCM token refreshed, re-registering',
        tag: _tag,
        metadata: {'tokenPreview': newToken.substring(0, 20)},
      );
      unawaited(_registerToken(newToken, reason: 'token_refresh'));
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

  /// Stop associating refresh events with the previous account and revoke the
  /// installation token so a later account receives a distinct FCM token.
  Future<void> deactivateForSignOut() async {
    _userId = null;
    _lastTokenSyncStartedAt = null;
    try {
      await FirebaseMessaging.instance.deleteToken().timeout(
        const Duration(seconds: 5),
      );
      AppLogger.info('FCM token revoked on sign-out', tag: _tag);
    } catch (error) {
      // Notification cleanup must never trap the user in a signed-in session.
      // The backend will still retire the stale token on its next failed send.
      AppLogger.warning(
        'Failed to revoke FCM token on sign-out',
        tag: _tag,
        metadata: {'reason': error.runtimeType.toString()},
      );
    }
  }

  /// Clear account ownership when auth becomes null outside the explicit
  /// sign-out path (for example, token revocation on another device).
  void clearUser() {
    _userId = null;
    _lastTokenSyncStartedAt = null;
  }

  /// Call on sign-out to clean up listeners.
  Future<void> dispose() async {
    await _tokenRefreshSubscription?.cancel();
    await _foregroundSubscription?.cancel();
    await _threadBodyTapSub?.cancel();
    _tokenRefreshSubscription = null;
    _foregroundSubscription = null;
    _threadBodyTapSub = null;
    _appLifecycleListener?.dispose();
    _appLifecycleListener = null;
    _tokenSyncInFlight = null;
    _lastTokenSyncStartedAt = null;
    _userId = null;
    _initialized = false;
    await _engagementTapController.close();
    await _signalNotificationTapController.close();
    await _threadFollowUpTapController.close();
    await _icebreakerTapController.close();
    await _dailyBriefingTapController.close();
    await _trackerUpdateTapController.close();
    await _chatReplyTapController.close();
    await _trialTapController.close();
    await _entitlementUpdatedController.close();
  }

  // Private helpers

  void _installLifecycleListener() {
    _appLifecycleListener ??= AppLifecycleListener(
      onResume: () {
        unawaited(_onAppResumed());
      },
    );
  }

  /// Resume handler for both the initialized and permission-denied cases.
  ///
  /// When initialized, this rechecks the FCM token so one invalidated by FCM, or
  /// a registration missed while offline, heals without a cold restart. When not
  /// initialized, the user previously denied the permission: if they have since
  /// granted it in OS Settings, finish initialization now rather than leaving
  /// them permanently unreachable.
  Future<void> _onAppResumed() async {
    if (_initialized) {
      await _syncCurrentToken(reason: 'app_resume');
      return;
    }

    final userId = _userId;
    if (userId == null) return;

    // getNotificationSettings reads the current OS state without prompting, so
    // this cannot re-trigger a dialog the user already dismissed.
    final settings =
        await FirebaseMessaging.instance.getNotificationSettings();
    final status = settings.authorizationStatus;
    if (status != AuthorizationStatus.authorized &&
        status != AuthorizationStatus.provisional) {
      return;
    }

    AppLogger.info(
      'Notification permission granted in OS settings, completing setup',
      tag: _tag,
      metadata: {'status': status.name, 'userId': userId},
    );
    await _completeInitialization(userId, status);
  }

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
    // Foreground pushes on Android are rendered by us (see
    // _handleForegroundMessage), so their taps arrive here rather than through
    // onMessageOpenedApp. Route them exactly like a background tap.
    final payload = response.payload;
    if (payload == null || payload.isEmpty) return;

    Map<String, dynamic> decoded;
    try {
      decoded = jsonDecode(payload) as Map<String, dynamic>;
    } catch (e) {
      AppLogger.warning(
        'Local notification tap payload could not be decoded',
        tag: _tag,
        metadata: {'error': e.toString()},
      );
      return;
    }

    final data = Map<String, dynamic>.from(
      (decoded['data'] as Map?) ?? const <String, dynamic>{},
    );
    _reportNotificationOpened(data);
    dispatchNotificationTap(data, fallbackBody: decoded['body'] as String?);
  }

  Future<void> _syncCurrentToken({
    required String reason,
    bool force = false,
  }) {
    if (_userId == null) return Future.value();

    final existing = _tokenSyncInFlight;
    if (existing != null) return existing;

    final now = DateTime.now();
    final lastStartedAt = _lastTokenSyncStartedAt;
    if (!force &&
        lastStartedAt != null &&
        now.difference(lastStartedAt) < _kTokenResumeSyncInterval) {
      return Future.value();
    }
    _lastTokenSyncStartedAt = now;

    late final Future<void> operation;
    operation = _fetchAndRegisterCurrentToken(reason).whenComplete(() {
      if (identical(_tokenSyncInFlight, operation)) {
        _tokenSyncInFlight = null;
      }
    });
    _tokenSyncInFlight = operation;
    return operation;
  }

  Future<void> _fetchAndRegisterCurrentToken(String reason) async {
    final uid = _userId;
    if (uid == null) return;

    // On the iOS simulator APNS is unavailable, so getToken() throws
    // firebase_messaging/apns-token-not-set. Treat that one case as expected.
    try {
      final token = await FirebaseMessaging.instance.getToken();
      if (token == null || _userId != uid) return;
      AppLogger.info(
        'FCM token retrieved',
        tag: _tag,
        metadata: {
          'reason': reason,
          'tokenPreview': token.substring(0, 20),
        },
      );
      await _registerToken(token, reason: reason);
    } on FirebaseException catch (error, stackTrace) {
      if (error.code == 'apns-token-not-set') {
        AppLogger.warning(
          'APNS token not set (expected on iOS simulator), skipping FCM token registration',
          tag: _tag,
          metadata: {'code': error.code, 'reason': reason},
        );
        return;
      }
      AppLogger.error(
        'Failed to retrieve FCM token',
        error: error,
        stackTrace: stackTrace,
        tag: _tag,
        metadata: {'reason': reason},
      );
    } catch (error, stackTrace) {
      AppLogger.error(
        'Failed to retrieve FCM token',
        error: error,
        stackTrace: stackTrace,
        tag: _tag,
        metadata: {'reason': reason},
      );
    }
  }

  Future<void> _registerToken(String token, {required String reason}) async {
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
        metadata: {
          'platform': platform,
          'reason': reason,
          'tokenPreview': token.substring(0, 20),
        },
      ),
      failure: (error) => AppLogger.error(
        'Failed to register FCM token',
        error: error,
        tag: _tag,
        metadata: {'reason': reason},
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

    // Entitlement sync push (billing webhook): the action is a refetch, there
    // is nothing to render. Emitted on RECEIPT, not on tap, so an open app
    // unlocks the moment payment lands in the browser next door.
    if (message.data['notification_type'] == kEntitlementUpdatedType) {
      _emitEntitlementUpdated(message.data);
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

    // iOS/macOS render the FCM banner themselves once these presentation
    // options are set. Android does NOT, and this API is a no-op there: Android
    // suppresses FCM's own display while the app is foregrounded, so unless the
    // banner is built locally the push is invisible to anyone with the app open.
    if (Platform.isIOS || Platform.isMacOS) {
      await FirebaseMessaging.instance
          .setForegroundNotificationPresentationOptions(
        alert: true,
        badge: true,
        sound: true,
      );
      return;
    }

    const androidDetails = AndroidNotificationDetails(
      _kAndroidChannelId,
      _kAndroidChannelName,
      importance: Importance.high,
      priority: Priority.high,
    );

    await _localNotificationsPlugin.show(
      id: _foregroundNotificationId(message),
      title: notification.title,
      body: notification.body,
      notificationDetails: const NotificationDetails(android: androidDetails),
      // The single payload slot has to carry everything the tap needs, since the
      // local plugin hands back only this string. Body included, because types
      // like `reminder` carry their text nowhere else.
      payload: jsonEncode({
        'data': message.data,
        'body': notification.body,
      }),
    );
  }

  /// Stable-per-message notification id.
  ///
  /// Keyed on the FCM message id so a redelivery of the same message replaces
  /// its banner instead of stacking a duplicate. `abs()` and the modulo keep it
  /// inside the 32-bit range Android requires.
  int _foregroundNotificationId(RemoteMessage message) {
    final key = message.messageId ?? message.hashCode.toString();
    return key.hashCode.abs() % 0x7FFFFFFF;
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
    dispatchNotificationTap(
      message.data,
      fallbackBody: message.notification?.body,
    );
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
    // Reported for EVERY notification type, not just signal-engine ones. The
    // backend stamps the ledger's notification_id into every payload
    // (notification_service.py) and the /events ingest path mirrors an opened
    // event into notification_ledger.record_tap regardless of source. Filtering
    // to signal_engine here left `outcome` and `led_to_session` unwritten for
    // every other push, which made "did this actually bring the user back"
    // unanswerable for precisely the notifications meant to bring them back.
    final notificationId = (data['notification_id'] as String?)?.trim() ??
        (data['content_id'] as String?)?.trim() ??
        '';
    if (notificationId.isEmpty) return;
    unawaited(_postSignalEvents([
      _buildEventPayload(
        eventType: 'notification_opened',
        // Ledger and outcome rows are both keyed on the notification_id; the
        // backend expects it in content_id so record_tap and resolve_outcome
        // find the right row.
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
  ///
  /// [fallbackBody] is the notification's own body text, used to seed a chat for
  /// types that carry no explicit `opening_chat_message`.
  @visibleForTesting
  void dispatchNotificationTap(
    Map<String, dynamic> data, {
    String? fallbackBody,
  }) {
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
          notificationReason: data['notification_reason'] as String? ?? '',
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
          notificationReason: data['notification_reason'] as String? ?? '',
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
          notificationReason: data['notification_reason'] as String? ?? '',
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
    } else if (notificationType == 'chat_reply') {
      // "Buddy replied" tapped: open the existing session and hydrate the reply that
      // finished server-side while the app was backgrounded (keyed by client_message_id).
      final cmid = data['cmid'] as String? ?? '';
      if (cmid.isNotEmpty) {
        _chatReplyTapController.add(ChatReplyTapPayload(
          clientMessageId: cmid,
          sessionId: data['session_id'] as String? ?? '',
        ));
      }
    } else if (notificationType == 'trial') {
      // Trial 3-days-left warning or trial-ended notice tapped: open the paywall,
      // contextualized by trial_variant.
      _trialTapController.add(TrialTapPayload(
        variant: data['trial_variant'] as String? ?? '',
      ));
    } else if (notificationType == kEntitlementUpdatedType) {
      // Entitlement-updated tapped (iOS shows it as a visible alert): same
      // refetch as the on-receipt path, no navigation.
      _emitEntitlementUpdated(data);
    } else {
      // Everything else opens a seeded chat. This branch is deliberately generic
      // rather than one arm per type: `welcome`, `reengage`, `session_followup`,
      // `intent_followup`, `reminder` and `memory_graph` all previously fell off
      // the end of this chain and dropped the user on the home screen's empty
      // state, which looked identical to a tap that worked. A new backend type
      // now lands somewhere sensible by default instead of silently nowhere.
      final opener = (data['opening_chat_message'] as String?)?.trim() ?? '';
      // The notification body is the last resort: types like `reminder` carry
      // their text only in the body, never in data.
      final seed = opener.isNotEmpty ? opener : (fallbackBody ?? '').trim();

      if (seed.isEmpty) {
        // Never silent. A tap that cannot be routed is a real defect, and it
        // must not be indistinguishable from a healthy tap in the logs.
        AppLogger.warning(
          'Notification tap could not be routed: no opener and no body',
          tag: _tag,
          metadata: {
            'notificationType': notificationType,
            'deepLink': data['deep_link'],
          },
        );
        return;
      }

      _engagementTapController.add(EngagementTapPayload(
        engagementId: '',
        initialMessage: seed,
        // Buddy-facing "why I reached out", so a tap into chat keeps Buddy
        // oriented instead of disowning its own opener.
        agentContext: (data['notification_reason'] as String?) ?? '',
      ));
    }
  }

  void _emitEntitlementUpdated(Map<String, dynamic> data) {
    AppLogger.info('Entitlement-updated push received', tag: _tag, metadata: {
      'tier': data['tier'],
      'status': data['status'],
    });
    _entitlementUpdatedController.add(EntitlementUpdatedPayload(
      tier: data['tier'] as String? ?? '',
      status: data['status'] as String? ?? '',
    ));
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
      notificationReason: data['notification_reason'] as String? ?? '',
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
