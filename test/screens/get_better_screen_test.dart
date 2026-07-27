import 'package:aura/data/models/get_better_feed.dart';
import 'package:aura/data/repositories/get_better_repository.dart';
import 'package:aura/presentation/screens/get_better/get_better_screen.dart';
import 'package:aura/presentation/viewmodels/auth_viewmodel.dart';
import 'package:aura/presentation/viewmodels/get_better_viewmodel.dart';
import 'package:aura/presentation/viewmodels/text_chat_viewmodel.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:mocktail/mocktail.dart';
import 'package:provider/provider.dart';

class _MockAuthViewModel extends Mock implements AuthViewModel {}

class _MockGetBetterViewModel extends Mock implements GetBetterViewModel {}

class _MockTextChatViewModel extends Mock implements TextChatViewModel {}

void main() {
  const banner = GetBetterIdea(
    id: 'steadier-start',
    title: 'A steadier start',
    category: 'Routines',
    summary: 'Make the first few minutes easier.',
    whyItFits: 'Small starts lower the pressure.',
    steps: ['Pick one action.'],
    chatPrompt: 'Help me choose a small start.',
    imageKey: 'routines',
    personalized: false,
    minutes: 5,
    storyVersion: 1,
    narrative: 'A calm opening can change the shape of the whole day.',
    whatItMeans: 'Momentum can begin with less pressure.',
    tryThis: 'Choose one action that takes under two minutes.',
    relatedStoryIds: [],
    cardType: 'banner',
    displayOrder: 0,
    featured: true,
  );

  late _MockAuthViewModel authViewModel;
  late _MockGetBetterViewModel getBetterViewModel;
  late _MockTextChatViewModel chatViewModel;

  setUpAll(() => registerFallbackValue(banner));

  setUp(() {
    authViewModel = _MockAuthViewModel();
    getBetterViewModel = _MockGetBetterViewModel();
    chatViewModel = _MockTextChatViewModel();

    when(() => authViewModel.user).thenReturn(null);
    when(() => getBetterViewModel.loading).thenReturn(false);
    when(() => getBetterViewModel.notice).thenReturn(null);
    when(() => getBetterViewModel.cachedImages).thenReturn(const {});
    when(() => getBetterViewModel.feed).thenReturn(
      GetBetterFeed(
        headline: 'A little better, your way',
        intro: 'Small stories for real life.',
        banner: banner,
        ideas: const [],
        nextCursor: 0,
        generatedAt: DateTime(2026, 7, 26),
        catalogVersion: 'test',
      ),
    );
    when(
      () => getBetterViewModel.recordOpened(
        any(),
        fromRelatedStory: any(named: 'fromRelatedStory'),
      ),
    ).thenAnswer((_) async {});
    when(() => getBetterViewModel.relatedStories(any())).thenReturn(const []);
    when(
      () => getBetterViewModel.storyState(any()),
    ).thenReturn(const GetBetterStoryState(saved: false, completed: false));
    when(() => chatViewModel.chatLimitReached).thenReturn(false);
    when(() => chatViewModel.error).thenReturn(null);
    when(() => chatViewModel.state).thenReturn(ViewState.idle);
  });

  testWidgets('opening a story keeps the route-scoped view model available', (
    tester,
  ) async {
    tester.view.physicalSize = const Size(1080, 2400);
    tester.view.devicePixelRatio = 1;
    addTearDown(() {
      tester.view.resetPhysicalSize();
      tester.view.resetDevicePixelRatio();
    });

    await tester.pumpWidget(
      MaterialApp(
        home: MultiProvider(
          providers: [
            ChangeNotifierProvider<AuthViewModel>.value(value: authViewModel),
            ChangeNotifierProvider<GetBetterViewModel>.value(
              value: getBetterViewModel,
            ),
            ChangeNotifierProvider<TextChatViewModel>.value(
              value: chatViewModel,
            ),
          ],
          child: const GetBetterScreen(),
        ),
      ),
    );
    await tester.pump();

    await tester.tap(find.text('A steadier start'));
    await tester.pumpAndSettle();

    expect(tester.takeException(), isNull);
    expect(
      find.text('A calm opening can change the shape of the whole day.'),
      findsOneWidget,
    );
    verify(
      () => getBetterViewModel.recordOpened(banner, fromRelatedStory: false),
    ).called(1);
  });
}
