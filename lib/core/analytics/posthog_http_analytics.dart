import 'dart:async';
import 'dart:convert';

import 'package:flutter/foundation.dart';
import 'package:http/http.dart' as http;
import 'package:shared_preferences/shared_preferences.dart';
import 'package:uuid/uuid.dart';

import '../../data/services/posthog_analytics_service.dart';
import 'analytics_client.dart';

/// PostHog capture over plain HTTP for platforms where posthog_flutter has no
/// implementation (Windows desktop). Same project token and event names as
/// mobile, so the funnel contract holds across platforms.
///
/// Fire-and-forget by design: a failed capture is dropped with a debug log and
/// never surfaces to the caller. Analytics must never break a user flow.
class PostHogHttpAnalytics implements AnalyticsClient {
  PostHogHttpAnalytics({
    required SharedPreferences prefs,
    http.Client? httpClient,
    Map<String, Object>? staticProperties,
  })  : _prefs = prefs,
        _http = httpClient ?? http.Client(),
        _staticProperties =
            staticProperties ?? const {'platform': 'windows', r'$os': 'Windows'};

  static const String _anonymousIdPrefsKey = 'posthog_http_anonymous_id';

  // Backpressure: if the network is down, don't stack up unbounded requests.
  // Overflow events are dropped, which is the correct failure mode for analytics.
  static const int _maxInFlightRequests = 20;

  final SharedPreferences _prefs;
  final http.Client _http;
  final Map<String, Object> _staticProperties;

  String? _identifiedUserId;
  int _inFlightRequests = 0;

  String get _distinctId => _identifiedUserId ?? _anonymousId;

  String get _anonymousId {
    final existing = _prefs.getString(_anonymousIdPrefsKey);
    if (existing != null) return existing;
    final generated = const Uuid().v4();
    unawaited(_prefs.setString(_anonymousIdPrefsKey, generated));
    return generated;
  }

  @override
  Future<void> trackEvent(String event, {Map<String, Object>? properties}) {
    return _capture(event, properties: properties);
  }

  @override
  Future<void> identifyUser(String uid, {Map<String, Object>? traits}) {
    final anonymousId = _anonymousId;
    _identifiedUserId = uid;
    // $identify merges the pre-auth anonymous events into the identified person.
    return _capture(r'$identify', properties: {
      r'$anon_distinct_id': anonymousId,
      r'$set': ?traits,
    });
  }

  @override
  Future<void> screenView(String screenName) {
    return _capture(r'$screen', properties: {r'$screen_name': screenName});
  }

  @override
  Future<void> reset() async {
    _identifiedUserId = null;
    await _prefs.remove(_anonymousIdPrefsKey);
  }

  Future<void> _capture(String event, {Map<String, Object>? properties}) async {
    if (_inFlightRequests >= _maxInFlightRequests) {
      debugPrint('[PostHogHttp] dropped $event (backpressure)');
      return;
    }
    _inFlightRequests++;
    try {
      final response = await _http
          .post(
            Uri.parse('${PostHogAnalyticsService.host}/capture/'),
            headers: {'Content-Type': 'application/json'},
            body: jsonEncode({
              'api_key': PostHogAnalyticsService.projectToken,
              'event': event,
              'distinct_id': _distinctId,
              'timestamp': DateTime.now().toUtc().toIso8601String(),
              'properties': {..._staticProperties, ...?properties},
            }),
          )
          .timeout(const Duration(seconds: 10));
      if (response.statusCode >= 400) {
        debugPrint('[PostHogHttp] $event -> HTTP ${response.statusCode}');
      }
    } catch (e) {
      debugPrint('[PostHogHttp] $event dropped: $e');
    } finally {
      _inFlightRequests--;
    }
  }
}
