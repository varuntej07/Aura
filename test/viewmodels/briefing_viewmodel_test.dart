import 'package:flutter_test/flutter_test.dart';
import 'package:mockito/annotations.dart';
import 'package:mockito/mockito.dart';

import 'package:aura/core/analytics/funnel_events.dart';
import 'package:aura/data/models/daily_briefing.dart';
import 'package:aura/data/services/backend_api_service.dart';
import 'package:aura/data/services/posthog_analytics_service.dart';
import 'package:aura/presentation/viewmodels/briefing_viewmodel.dart';
import 'package:aura/presentation/viewmodels/view_state.dart';

import 'briefing_viewmodel_test.mocks.dart';

/// Behavioral coverage for the briefing screen's ViewModel: a successful load
/// populates the briefing and fires the `briefing_opened` funnel step exactly
/// once; an empty (null) response yields the empty state and fires nothing (so an
/// empty open never counts as a conversion).
@GenerateNiceMocks([
  MockSpec<BackendApiService>(),
  MockSpec<PostHogAnalyticsService>(),
])
void main() {
  late MockBackendApiService backend;
  late MockPostHogAnalyticsService analytics;
  late BriefingViewModel sut;

  setUp(() {
    backend = MockBackendApiService();
    analytics = MockPostHogAnalyticsService();
    sut = BriefingViewModel(
      backendApiService: backend,
      postHogAnalyticsService: analytics,
    );
  });

  const testBriefing = DailyBriefing(
    date: '2026-06-13',
    narrative: 'Morning. Your F1 corner had a wild one.',
    chatSeedMessage: 'The Verstappen win. Want the detail?',
    sources: [
      BriefingSource(
        title: 'Verstappen wins Monaco',
        url: 'https://example.com/1',
        source: 'google_news',
        category: 'sports',
      ),
    ],
  );

  test('load() populates the briefing and fires briefing_opened once', () async {
    when(backend.fetchTodayBriefing()).thenAnswer((_) async => testBriefing);

    await sut.load();

    expect(sut.state, ViewState.loaded);
    expect(sut.briefing, isNotNull);
    expect(sut.briefing!.narrative, contains('F1 corner'));
    expect(sut.isEmpty, isFalse);

    verify(analytics.trackEvent(
      FunnelEvents.briefingOpened,
      properties: argThat(
        containsPair(
          FunnelEvents.propNotificationOrigin,
          FunnelEvents.originBriefing,
        ),
        named: 'properties',
      ),
    )).called(1);
  });

  test('load() with no briefing yields empty state and fires nothing', () async {
    when(backend.fetchTodayBriefing()).thenAnswer((_) async => null);

    await sut.load();

    expect(sut.state, ViewState.loaded);
    expect(sut.briefing, isNull);
    expect(sut.isEmpty, isTrue);

    verifyNever(analytics.trackEvent(any, properties: anyNamed('properties')));
  });

  test('fetchWorldNow() populates the world snapshot and fires world_briefing_fetched once',
      () async {
    when(backend.fetchWorldBriefing(refresh: anyNamed('refresh')))
        .thenAnswer((_) async => testBriefing);

    await sut.fetchWorldNow();

    expect(sut.briefing, isNotNull);
    expect(sut.isWorldSnapshot, isTrue);
    expect(sut.fetchingWorld, isFalse);
    expect(sut.worldError, isNull);
    expect(sut.state, ViewState.loaded);

    verify(analytics.trackEvent(
      FunnelEvents.worldBriefingFetched,
      properties: argThat(
        containsPair(
          FunnelEvents.propNotificationOrigin,
          FunnelEvents.originBriefing,
        ),
        named: 'properties',
      ),
    )).called(1);
  });

  test('fetchWorldNow() failure sets a casual error and fires nothing', () async {
    when(backend.fetchWorldBriefing(refresh: anyNamed('refresh')))
        .thenAnswer((_) async => null);

    await sut.fetchWorldNow();

    expect(sut.briefing, isNull);
    expect(sut.isWorldSnapshot, isFalse);
    expect(sut.fetchingWorld, isFalse);
    expect(sut.worldError, isNotNull);

    verifyNever(analytics.trackEvent(any, properties: anyNamed('properties')));
  });

  test('load() generates on demand when nothing is stored for today', () async {
    when(backend.fetchTodayBriefing()).thenAnswer((_) async => null);
    when(backend.generateTodayBriefing(force: anyNamed('force')))
        .thenAnswer((_) async => testBriefing);

    await sut.load();

    expect(sut.briefing, isNotNull);
    expect(sut.isEmpty, isFalse);
    verify(backend.generateTodayBriefing(force: false)).called(1);
  });

}
