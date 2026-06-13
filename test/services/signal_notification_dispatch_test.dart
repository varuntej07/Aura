import 'package:flutter_test/flutter_test.dart';
import 'package:mockito/annotations.dart';

import 'package:aura/core/analytics/funnel_events.dart';
import 'package:aura/core/network/api_client.dart';
import 'package:aura/data/services/notification_service.dart';
import 'package:aura/data/services/posthog_analytics_service.dart';

import 'signal_notification_dispatch_test.mocks.dart';

/// Routing coverage for signal-engine content notifications: tapping one must
/// emit a [SignalNotificationTapPayload] carrying the funnel join keys so the
/// chat surface can attribute the resulting session + first reply back to the
/// originating send. An empty notificationId must NOT emit — there'd be nothing
/// to join the funnel on.
@GenerateNiceMocks([
  MockSpec<ApiClient>(),
  MockSpec<PostHogAnalyticsService>(),
])
void main() {
  late NotificationService sut;

  setUp(() {
    sut = NotificationService(
      apiClient: MockApiClient(),
      postHogAnalyticsService: MockPostHogAnalyticsService(),
    );
  });

  tearDown(() async {
    await sut.dispose();
  });

  test('signal_engine tap emits a payload with the funnel join keys', () async {
    final received = <SignalNotificationTapPayload>[];
    sut.signalNotificationTapStream.listen(received.add);

    sut.dispatchNotificationTap({
      'notification_type': FunnelEvents.originSignalEngine,
      FunnelEvents.propNotificationId: 'notif-1',
      FunnelEvents.propContentId: 'content-1',
      FunnelEvents.propCategory: 'tech',
      'opening_chat_message': 'Saw this and thought of you.',
    });

    await Future<void>.delayed(Duration.zero);

    expect(received, hasLength(1));
    expect(received.first.notificationId, 'notif-1');
    expect(received.first.contentId, 'content-1');
    expect(received.first.category, 'tech');
    expect(received.first.openingChatMessage, 'Saw this and thought of you.');
  });

  test('carries content_kind and url for read-vs-discuss routing', () async {
    final received = <SignalNotificationTapPayload>[];
    sut.signalNotificationTapStream.listen(received.add);

    sut.dispatchNotificationTap({
      'notification_type': FunnelEvents.originSignalEngine,
      FunnelEvents.propNotificationId: 'notif-2',
      FunnelEvents.propContentId: 'content-2',
      FunnelEvents.propCategory: 'news',
      'opening_chat_message': 'Saw this.',
      'content_kind': 'read',
      'url': 'https://example.com/a',
    });

    await Future<void>.delayed(Duration.zero);

    expect(received, hasLength(1));
    expect(received.first.contentKind, 'read');
    expect(received.first.url, 'https://example.com/a');
  });

  test('does not emit when notificationId is empty', () async {
    final received = <SignalNotificationTapPayload>[];
    sut.signalNotificationTapStream.listen(received.add);

    sut.dispatchNotificationTap({
      'notification_type': FunnelEvents.originSignalEngine,
      FunnelEvents.propNotificationId: '',
      FunnelEvents.propContentId: 'content-1',
      FunnelEvents.propCategory: 'tech',
      'opening_chat_message': 'Hey',
    });

    await Future<void>.delayed(Duration.zero);

    expect(received, isEmpty);
  });

  test('does not emit when notificationId is absent', () async {
    final received = <SignalNotificationTapPayload>[];
    sut.signalNotificationTapStream.listen(received.add);

    sut.dispatchNotificationTap({
      'notification_type': FunnelEvents.originSignalEngine,
      FunnelEvents.propContentId: 'content-1',
    });

    await Future<void>.delayed(Duration.zero);

    expect(received, isEmpty);
  });
}
