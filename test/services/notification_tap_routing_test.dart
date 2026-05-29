import 'package:flutter_test/flutter_test.dart';
import 'package:mocktail/mocktail.dart';

import 'package:aura/core/network/api_client.dart';
import 'package:aura/data/services/notification_service.dart';
import 'package:aura/data/services/posthog_analytics_service.dart';

class MockApiClient extends Mock implements ApiClient {}

class MockPostHogAnalyticsService extends Mock implements PostHogAnalyticsService {}

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

  group('dispatchNotificationTap — daily_nudge', () {
    test('emits on engagementTapStream when initial_message is present', () async {
      final received = <EngagementTapPayload>[];
      sut.engagementTapStream.listen(received.add);

      sut.dispatchNotificationTap({
        'notification_type': 'daily_nudge',
        'initial_message': 'Good morning! Here is your daily briefing.',
      });

      await Future<void>.delayed(Duration.zero);

      expect(received, hasLength(1));
      expect(received.first.initialMessage, 'Good morning! Here is your daily briefing.');
    });

    test('engagementId is empty (no engagement tracking for daily nudges)', () async {
      final received = <EngagementTapPayload>[];
      sut.engagementTapStream.listen(received.add);

      sut.dispatchNotificationTap({
        'notification_type': 'daily_nudge',
        'initial_message': 'Your morning update is ready.',
      });

      await Future<void>.delayed(Duration.zero);

      expect(received.first.engagementId, isEmpty);
      expect(received.first.agentContext, isEmpty);
    });

    test('does not emit when initial_message is absent', () async {
      final received = <EngagementTapPayload>[];
      sut.engagementTapStream.listen(received.add);

      sut.dispatchNotificationTap({
        'notification_type': 'daily_nudge',
      });

      await Future<void>.delayed(Duration.zero);

      expect(received, isEmpty);
    });

    test('does not emit when initial_message is empty string', () async {
      final received = <EngagementTapPayload>[];
      sut.engagementTapStream.listen(received.add);

      sut.dispatchNotificationTap({
        'notification_type': 'daily_nudge',
        'initial_message': '',
      });

      await Future<void>.delayed(Duration.zero);

      expect(received, isEmpty);
    });
  });

  group('dispatchNotificationTap — meeting_reminder', () {
    test('emits on engagementTapStream when initial_message is present', () async {
      final received = <EngagementTapPayload>[];
      sut.engagementTapStream.listen(received.add);

      sut.dispatchNotificationTap({
        'notification_type': 'meeting_reminder',
        'initial_message': 'Your standup starts in 15 minutes.',
      });

      await Future<void>.delayed(Duration.zero);

      expect(received, hasLength(1));
      expect(received.first.initialMessage, 'Your standup starts in 15 minutes.');
    });

    test('does not emit when initial_message is missing', () async {
      final received = <EngagementTapPayload>[];
      sut.engagementTapStream.listen(received.add);

      sut.dispatchNotificationTap({'notification_type': 'meeting_reminder'});

      await Future<void>.delayed(Duration.zero);

      expect(received, isEmpty);
    });
  });

  group('dispatchNotificationTap — engagement (regression)', () {
    test('emits on engagementTapStream with full payload', () async {
      final received = <EngagementTapPayload>[];
      sut.engagementTapStream.listen(received.add);

      sut.dispatchNotificationTap({
        'notification_type': 'engagement',
        'engagement_id': 'eng-abc',
        'initial_message': 'How are you feeling today?',
        'agent_context': 'wellness',
      });

      await Future<void>.delayed(Duration.zero);

      expect(received, hasLength(1));
      expect(received.first.engagementId, 'eng-abc');
      expect(received.first.initialMessage, 'How are you feeling today?');
      expect(received.first.agentContext, 'wellness');
    });

    test('does not emit when engagement_id is missing', () async {
      final received = <EngagementTapPayload>[];
      sut.engagementTapStream.listen(received.add);

      sut.dispatchNotificationTap({
        'notification_type': 'engagement',
        'initial_message': 'Hey!',
      });

      await Future<void>.delayed(Duration.zero);

      expect(received, isEmpty);
    });
  });

  group('dispatchNotificationTap — agent_nudge (regression)', () {
    test('emits on agentNudgeTapStream with agentId and chatOpener', () async {
      final received = <AgentNudgeTapPayload>[];
      sut.agentNudgeTapStream.listen(received.add);

      sut.dispatchNotificationTap({
        'notification_type': 'agent_nudge',
        'agent_id': 'sports',
        'opening_chat_message': 'Big match tonight!',
      });

      await Future<void>.delayed(Duration.zero);

      expect(received, hasLength(1));
      expect(received.first.agentId, 'sports');
      expect(received.first.chatOpener, 'Big match tonight!');
    });

    test('does not emit when agent_id is missing', () async {
      final received = <AgentNudgeTapPayload>[];
      sut.agentNudgeTapStream.listen(received.add);

      sut.dispatchNotificationTap({'notification_type': 'agent_nudge'});

      await Future<void>.delayed(Duration.zero);

      expect(received, isEmpty);
    });
  });

  group('dispatchNotificationTap — unknown type', () {
    test('emits nothing on either stream for an unrecognised notification_type', () async {
      final engagements = <EngagementTapPayload>[];
      final nudges = <AgentNudgeTapPayload>[];
      sut.engagementTapStream.listen(engagements.add);
      sut.agentNudgeTapStream.listen(nudges.add);

      sut.dispatchNotificationTap({
        'notification_type': 'reminder',
        'initial_message': 'Pick up groceries',
      });

      await Future<void>.delayed(Duration.zero);

      expect(engagements, isEmpty);
      expect(nudges, isEmpty);
    });

    test('emits nothing when notification_type key is absent', () async {
      final engagements = <EngagementTapPayload>[];
      sut.engagementTapStream.listen(engagements.add);

      sut.dispatchNotificationTap({'initial_message': 'Some message'});

      await Future<void>.delayed(Duration.zero);

      expect(engagements, isEmpty);
    });
  });
}
