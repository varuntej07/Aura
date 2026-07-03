import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:aura/core/analytics/posthog_http_analytics.dart';
import 'package:aura/data/services/posthog_analytics_service.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  Future<SharedPreferences> freshPrefs() async {
    SharedPreferences.setMockInitialValues({});
    return SharedPreferences.getInstance();
  }

  MockClient capturingClient(List<Map<String, dynamic>> sink,
      {int statusCode = 200}) {
    return MockClient((request) async {
      sink.add(jsonDecode(request.body) as Map<String, dynamic>);
      return http.Response('{"status": 1}', statusCode);
    });
  }

  group('PostHogHttpAnalytics', () {
    test('capture carries api_key, event, platform property, stable anon id',
        () async {
      final captured = <Map<String, dynamic>>[];
      final analytics = PostHogHttpAnalytics(
        prefs: await freshPrefs(),
        httpClient: capturingClient(captured),
      );

      await analytics.trackEvent('desktop_app_open');
      await analytics.trackEvent('voice_error', properties: {'code': 'x'});

      expect(captured, hasLength(2));
      expect(captured[0]['api_key'], PostHogAnalyticsService.projectToken);
      expect(captured[0]['event'], 'desktop_app_open');
      expect(captured[0]['properties']['platform'], 'windows');
      expect(captured[1]['properties']['code'], 'x');
      // Anonymous distinct id is stable across events.
      expect(captured[0]['distinct_id'], isNotEmpty);
      expect(captured[0]['distinct_id'], captured[1]['distinct_id']);
    });

    test('identify switches distinct_id to uid and links the anon id',
        () async {
      final captured = <Map<String, dynamic>>[];
      final analytics = PostHogHttpAnalytics(
        prefs: await freshPrefs(),
        httpClient: capturingClient(captured),
      );

      await analytics.trackEvent('pre_auth_event');
      final anonId = captured[0]['distinct_id'] as String;

      await analytics.identifyUser('uid-123');
      await analytics.trackEvent('post_auth_event');

      final identify = captured[1];
      expect(identify['event'], r'$identify');
      expect(identify['distinct_id'], 'uid-123');
      expect(identify['properties'][r'$anon_distinct_id'], anonId);
      expect(captured[2]['distinct_id'], 'uid-123');
    });

    test('screenView maps to the \$screen event', () async {
      final captured = <Map<String, dynamic>>[];
      final analytics = PostHogHttpAnalytics(
        prefs: await freshPrefs(),
        httpClient: capturingClient(captured),
      );

      await analytics.screenView('overlay_panel');

      expect(captured.single['event'], r'$screen');
      expect(captured.single['properties'][r'$screen_name'], 'overlay_panel');
    });

    test('network failure is swallowed, never thrown', () async {
      final analytics = PostHogHttpAnalytics(
        prefs: await freshPrefs(),
        httpClient: MockClient((_) async => throw Exception('offline')),
      );

      await expectLater(analytics.trackEvent('anything'), completes);
    });

    test('HTTP error status is swallowed, never thrown', () async {
      final captured = <Map<String, dynamic>>[];
      final analytics = PostHogHttpAnalytics(
        prefs: await freshPrefs(),
        httpClient: capturingClient(captured, statusCode: 503),
      );

      await expectLater(analytics.trackEvent('anything'), completes);
    });

    test('reset returns to a fresh anonymous identity', () async {
      final captured = <Map<String, dynamic>>[];
      final analytics = PostHogHttpAnalytics(
        prefs: await freshPrefs(),
        httpClient: capturingClient(captured),
      );

      await analytics.trackEvent('first');
      final firstAnonId = captured[0]['distinct_id'] as String;
      await analytics.identifyUser('uid-123');
      await analytics.reset();
      await analytics.trackEvent('after_reset');

      final afterReset = captured.last;
      expect(afterReset['distinct_id'], isNot('uid-123'));
      expect(afterReset['distinct_id'], isNot(firstAnonId));
    });
  });
}
