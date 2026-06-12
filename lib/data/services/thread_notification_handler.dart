import 'dart:async';
import 'dart:convert';

import 'package:firebase_auth/firebase_auth.dart';
import 'package:firebase_messaging/firebase_messaging.dart';
import 'package:flutter/foundation.dart';
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
/// the LLM's suggestion chips. Tapping a chip (or typing) posts the answer to
/// `/threads/reply` and re-posts the notification showing Buddy's reply — the
/// app never opens. iOS has no dynamic chips, so it shows a plain push and the
/// pills render in-chat on tap (handled by [NotificationService]).
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
  final question = data['question'] as String? ?? '';
  if (threadId.isEmpty || question.isEmpty) return;

  final replies = _decodeSuggestedReplies(data);

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
        // RemoteInput: tapping shows the suggestion chips and a free-form field.
        inputs: <AndroidNotificationActionInput>[
          AndroidNotificationActionInput(
            label: 'Tell Buddy',
            choices: replies,
            allowFreeFormInput: true,
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
    // slot — including the chips, so a body tap can open chat with the pills.
    payload: jsonEncode({
      'thread_id': threadId,
      'question': question,
      'suggested_replies': replies,
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
void threadReplyNotificationBackground(NotificationResponse response) {
  // Fire-and-forget: the isolate stays alive for the returned future.
  handleThreadReplyResponse(response);
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
  try {
    final decoded = jsonDecode(payload) as Map<String, dynamic>;
    threadId = decoded['thread_id'] as String? ?? '';
    question = decoded['question'] as String? ?? '';
  } catch (_) {
    return;
  }
  if (threadId.isEmpty) return;

  final buddyReply = await _postThreadReply(
    threadId: threadId,
    question: question,
    reply: reply,
  );

  // Re-post the same notification id with Buddy's reply so the conversation
  // continues in the shade. On failure, show a gentle, honest fallback rather
  // than leaving the user wondering if their reply vanished.
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
    body: buddyReply ?? "got it. i'll pick this up with you in the app.",
    notificationDetails: const NotificationDetails(android: androidDetails),
    payload: jsonEncode({'thread_id': threadId, 'question': question}),
  );
}

/// POST the answer to `/threads/reply` with the user's Firebase token.
/// Returns Buddy's reply text, or null on any failure.
Future<String?> _postThreadReply({
  required String threadId,
  required String question,
  required String reply,
}) async {
  try {
    await FirebaseConfig.initialize();
    final token = await FirebaseAuth.instance.currentUser?.getIdToken();
    if (token == null) {
      AppLogger.warning('No auth token in isolate; cannot post reply', tag: _tag);
      return null;
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
      return null;
    }
    final body = jsonDecode(res.body) as Map<String, dynamic>;
    return body['reply'] as String?;
  } catch (e) {
    AppLogger.warning('Thread reply post failed', tag: _tag, metadata: {'error': e.toString()});
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
