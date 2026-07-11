import 'dart:async';
import 'dart:convert';

import 'package:firebase_auth/firebase_auth.dart';
import 'package:firebase_messaging/firebase_messaging.dart';
import 'package:flutter_local_notifications/flutter_local_notifications.dart';
import 'package:http/http.dart' as http;

import '../../core/config/environment.dart';
import '../../core/config/firebase_config.dart';
import '../../core/logging/app_logger.dart';

/// Self-contained handler for `thread_followup` curiosity notifications — the
/// silent shade-reply path.
///
/// Why this lives apart from [NotificationService]: the inline-reply action can
/// fire in a **background isolate** (app terminated), where there is no running
/// app, no DI container, and no [NotificationService] instance. Everything the
/// isolate needs (Firebase init, auth token, HTTP, notification re-post) is
/// therefore packaged here as top-level functions with no app-state dependency.
///
/// Android: the notification carries a `Reply` action backed by RemoteInput with
/// the LLM's suggestion chips (send-only). Tapping a chip optimistically re-posts
/// the user's own words straight away (clearing Android's "sending" spinner),
/// posts the answer to `/threads/reply`, and Buddy's reply arrives as its own
/// follow-up push that updates the same notification — so it lands even if this
/// isolate is reaped the instant the request flushes. The app never opens. iOS
/// has no dynamic chips, so it shows a plain push and the pills render in-chat on
/// tap (handled by [NotificationService]).
///
/// VERIFICATION: the RemoteInput + background-isolate flow only runs on a
/// physical Android device. Run `flutter analyze` and dogfood on-device before
/// trusting this path.
const String kThreadFollowUpType = 'thread_followup';

const String _tag = 'ThreadNotification';

const String _kReplyActionId = 'thread_reply';

/// Single notification channel reused from the backend contract.
const String _kAndroidChannelId = 'aura_default';

const String _kThreadReplyEndpoint = '/threads/reply';

final FlutterLocalNotificationsPlugin _isolatePlugin =
    FlutterLocalNotificationsPlugin();

/// Stable, positive 31-bit notification id derived from the thread id, so a
/// re-post (Buddy's reply) replaces the same notification rather than stacking.
int _notificationIdForThread(String threadId) => threadId.hashCode & 0x7fffffff;

/// True when this FCM message is a curiosity follow-up.
bool isThreadFollowUp(RemoteMessage message) =>
    message.data['notification_type'] == kThreadFollowUpType;

/// Register the local-notifications plugin (and its background action callback)
/// at app startup so terminated-state inline replies are handled. Idempotent.
Future<void> ensureThreadNotificationsInitialized() =>
    _ensureIsolatePluginInitialized();

List<String> _decodeSuggestedReplies(Map<String, dynamic> data) {
  final raw = data['suggested_replies'];
  if (raw is! String || raw.isEmpty) return const [];
  try {
    final decoded = jsonDecode(raw);
    if (decoded is List) {
      return decoded.map((e) => e.toString()).toList();
    }
  } catch (_) {
    // A malformed payload just means no chips — never throw out of a handler.
  }
  return const [];
}

/// Build and display the interactive chip notification (Android).
///
/// Safe to call from the background isolate or the foreground service; it owns
/// its own plugin instance so it never depends on [NotificationService] state.
Future<void> showThreadFollowUpNotification(RemoteMessage message) async {
  final data = message.data;
  final threadId = data['thread_id'] as String? ?? '';
  if (threadId.isEmpty) return;

  // Buddy's async reply to a shade answer rides this same type tagged
  // kind=reply: update the SAME notification with his words and no chips (the
  // conversation continues in the app on a body tap).
  final buddyReply = data['buddy_reply'] as String? ?? '';
  if (data['kind'] == 'reply' || buddyReply.isNotEmpty) {
    if (buddyReply.isEmpty) return;
    await _repostThreadConversation(threadId: threadId, body: buddyReply);
    return;
  }

  final question = data['question'] as String? ?? '';
  if (question.isEmpty) return;

  final replies = _decodeSuggestedReplies(data);
  final notificationReason = data['notification_reason'] as String? ?? '';

  await _ensureIsolatePluginInitialized();

  final androidDetails = AndroidNotificationDetails(
    _kAndroidChannelId,
    'Aura Notifications',
    importance: Importance.high,
    priority: Priority.high,
    actions: <AndroidNotificationAction>[
      AndroidNotificationAction(
        _kReplyActionId,
        'Reply',
        // RemoteInput with choices only (no free-form field). With
        // allowFreeFormInput:false Android renders the choices as instant-send
        // buttons: tapping a chip dispatches the action immediately instead of
        // loading the chip text into an editable field the user must then send.
        // Free-form typing lives on the body-tap -> chat path instead.
        inputs: <AndroidNotificationActionInput>[
          AndroidNotificationActionInput(
            label: 'Tell Buddy',
            choices: replies,
            allowFreeFormInput: false,
          ),
        ],
        // Handle in the background isolate — do not launch the UI, keep the
        // notification up until we re-post Buddy's reply.
        showsUserInterface: false,
        cancelNotification: false,
      ),
    ],
  );

  await _isolatePlugin.show(
    id: _notificationIdForThread(threadId),
    title: 'Buddy',
    body: question,
    notificationDetails: NotificationDetails(android: androidDetails),
    // Carry everything the tap/reply handlers need through the single payload
    // slot, including the chips, so a body tap can open chat with the pills (the
    // path for a free-form / custom reply, since the shade chips are send-only).
    payload: jsonEncode({
      'thread_id': threadId,
      'question': question,
      'suggested_replies': replies,
      'notification_reason': notificationReason,
    }),
  );
}

/// Emits when the user taps the notification BODY (not the Reply action).
///
/// The follow-up notification is built locally, so a body tap is delivered to
/// the local-notifications handler — not `FirebaseMessaging.onMessageOpenedApp`
/// — and would otherwise be dropped. [NotificationService] relays this into its
/// own tap stream so the app opens chat seeded with the question + pills.
final _bodyTapController = StreamController<Map<String, dynamic>>.broadcast();
Stream<Map<String, dynamic>> get threadBodyTapStream => _bodyTapController.stream;

void _emitBodyTap(String? payload) {
  if (payload == null || payload.isEmpty) return;
  try {
    final decoded = jsonDecode(payload) as Map<String, dynamic>;
    final threadId = decoded['thread_id'] as String? ?? '';
    if (threadId.isEmpty) return;
    _bodyTapController.add({
      'thread_id': threadId,
      'question': decoded['question'] as String? ?? '',
      'suggested_replies':
          (decoded['suggested_replies'] as List?)?.map((e) => e.toString()).toList() ??
              const <String>[],
      'notification_reason': decoded['notification_reason'] as String? ?? '',
    });
  } catch (_) {
    // Malformed payload just means no routing — never throw out of a tap.
  }
}

/// Route a terminated-state launch: tapping a locally-built notification while
/// the app is killed launches it without firing the response callback, so the
/// launch must be read explicitly at startup. Call once during init.
Future<void> handleThreadNotificationColdLaunch() async {
  await _ensureIsolatePluginInitialized();
  final details = await _isolatePlugin.getNotificationAppLaunchDetails();
  if (details == null || !details.didNotificationLaunchApp) return;
  final response = details.notificationResponse;
  if (response == null) return;
  // Only a body tap launches the app — the Reply action is showsUserInterface:
  // false, so it never reaches here.
  if (response.actionId == _kReplyActionId) return;
  _emitBodyTap(response.payload);
}

/// Background-isolate entry point for the inline reply action.
///
/// Registered via `onDidReceiveBackgroundNotificationResponse`. Must be
/// top-level and annotated so tree-shaking keeps it.
@pragma('vm:entry-point')
Future<void> threadReplyNotificationBackground(NotificationResponse response) async {
  // Await so this isolate stays alive through the optimistic echo (which clears
  // the chip spinner) and the flush of the reply request. A fire-and-forget call
  // could tear the isolate down before the echo, leaving the chip spinning.
  await handleThreadReplyResponse(response);
}

/// Shared reply handling, callable from foreground or background.
///
/// Reads the chosen chip / typed text, posts it to the backend, and re-posts
/// the notification with Buddy's reply. Returns early (never throws) on anything
/// it cannot act on, so a stray response can never crash the isolate.
Future<void> handleThreadReplyResponse(NotificationResponse response) async {
  if (response.actionId != _kReplyActionId) {
    // A plain notification body tap (no action). Only reached in the main
    // isolate — a body tap that resumes/launches the app — so emitting to the
    // body-tap stream reaches the app, which opens chat seeded with the pills.
    _emitBodyTap(response.payload);
    return;
  }

  final reply = response.input?.trim() ?? '';
  final payload = response.payload;
  if (reply.isEmpty || payload == null || payload.isEmpty) return;

  String threadId;
  String question;
  String notificationReason;
  try {
    final decoded = jsonDecode(payload) as Map<String, dynamic>;
    threadId = decoded['thread_id'] as String? ?? '';
    question = decoded['question'] as String? ?? '';
    notificationReason = decoded['notification_reason'] as String? ?? '';
  } catch (_) {
    return;
  }
  if (threadId.isEmpty) return;

  // 1. Optimistic echo FIRST. Android keeps a "sending" spinner on the chip
  //    until this notification is updated, so doing it before any auth/network/
  //    LLM work clears it in well under a second — and it survives this isolate
  //    being reaped mid-request. This is the WhatsApp move: your own words land
  //    instantly, the reply follows.
  try {
    await _repostThreadConversation(
      threadId: threadId,
      body: reply,
      question: question,
      notificationReason: notificationReason,
    );
  } catch (e) {
    // If even the echo failed, cancel the notification so the spinner can never
    // hang — a cleared notification beats a stuck one.
    try {
      await _isolatePlugin.cancel(id: _notificationIdForThread(threadId));
    } catch (_) {}
    AppLogger.warning(
      'Thread optimistic echo failed',
      tag: _tag,
      metadata: {'error': e.toString()},
    );
  }

  // 2. Deliver the answer to the backend. Buddy's reply comes back as its OWN
  //    follow-up push (handled by showThreadFollowUpNotification) that updates
  //    this same notification, so it lands even if this isolate is gone the
  //    moment the request flushes. We deliberately do not re-post Buddy's reply
  //    here, and we leave the echo in place on failure rather than clobber a
  //    reply push that may already be arriving.
  final sent = await _postThreadReply(
    threadId: threadId,
    question: question,
    reply: reply,
  );
  if (!sent) {
    AppLogger.warning(
      'Thread reply not delivered; optimistic echo left in shade',
      tag: _tag,
      metadata: {'thread_id': threadId},
    );
  }
}

/// Re-post the thread notification as a plain (no-action) message with [body],
/// replacing whatever is shown for this thread (same stable id). Used for the
/// optimistic echo of the user's reply and for Buddy's incoming reply push.
Future<void> _repostThreadConversation({
  required String threadId,
  required String body,
  String question = '',
  String notificationReason = '',
}) async {
  await _ensureIsolatePluginInitialized();
  const androidDetails = AndroidNotificationDetails(
    _kAndroidChannelId,
    'Aura Notifications',
    importance: Importance.high,
    priority: Priority.high,
  );
  await _isolatePlugin.show(
    id: _notificationIdForThread(threadId),
    title: 'Buddy',
    body: body,
    notificationDetails: const NotificationDetails(android: androidDetails),
    payload: jsonEncode({
      'thread_id': threadId,
      'question': question,
      'notification_reason': notificationReason,
    }),
  );
}

/// POST the answer to `/threads/reply` with the user's Firebase token.
///
/// Returns true if the backend accepted it (HTTP 200), false on any failure.
/// Buddy's reply is delivered separately as a follow-up push, so its text is no
/// longer read from this response.
Future<bool> _postThreadReply({
  required String threadId,
  required String question,
  required String reply,
}) async {
  try {
    await FirebaseConfig.initialize();
    final token = await _resolveIsolateToken();
    if (token == null) {
      AppLogger.warning('No auth token in isolate; cannot post reply', tag: _tag);
      return false;
    }

    final uri = Uri.parse('${Environment.current.apiBaseUrl}$_kThreadReplyEndpoint');
    final res = await http
        .post(
          uri,
          headers: {
            'Content-Type': 'application/json',
            'Authorization': 'Bearer $token',
          },
          body: jsonEncode({
            'thread_id': threadId,
            'question': question,
            'reply': reply,
          }),
        )
        .timeout(const Duration(seconds: 12));

    if (res.statusCode != 200) {
      AppLogger.warning(
        'Thread reply rejected',
        tag: _tag,
        metadata: {'status': res.statusCode},
      );
      return false;
    }
    return true;
  } catch (e) {
    AppLogger.warning('Thread reply post failed', tag: _tag, metadata: {'error': e.toString()});
    return false;
  }
}

/// Fetch the Firebase ID token inside a background isolate.
///
/// A cold isolate restores persisted auth state asynchronously, so
/// `currentUser` is briefly null right after `FirebaseConfig.initialize()`.
/// Reading it immediately (as the old code did) made the reply silently fail
/// with "no auth token". We use the current user when it is already present, and
/// otherwise wait for the first restored signed-in state, bounded by a timeout
/// so a genuinely signed-out isolate never hangs.
Future<String?> _resolveIsolateToken() async {
  var user = FirebaseAuth.instance.currentUser;
  user ??= await FirebaseAuth.instance
      .authStateChanges()
      .firstWhere((u) => u != null)
      .timeout(const Duration(seconds: 5), onTimeout: () => null);
  if (user == null) return null;
  try {
    return await user.getIdToken();
  } catch (e) {
    AppLogger.warning('getIdToken failed in isolate', tag: _tag, metadata: {'error': e.toString()});
    return null;
  }
}

bool _isolatePluginReady = false;

Future<void> _ensureIsolatePluginInitialized() async {
  if (_isolatePluginReady) return;
  const initSettings = InitializationSettings(
    android: AndroidInitializationSettings('@drawable/ic_notification'),
    iOS: DarwinInitializationSettings(),
  );
  await _isolatePlugin.initialize(
    settings: initSettings,
    onDidReceiveNotificationResponse: handleThreadReplyResponse,
    onDidReceiveBackgroundNotificationResponse: threadReplyNotificationBackground,
  );
  _isolatePluginReady = true;
}
