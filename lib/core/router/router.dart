import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';
import 'package:provider/provider.dart';

import '../analytics/app_route_observer.dart';
import '../logging/app_logger.dart';

import '../../data/repositories/agent_suggestion_pills_repository.dart';
import '../../data/repositories/chat_repository.dart';
import '../../data/services/backend_api_service.dart';
import '../../data/services/buddy_pills_refresher.dart';
import '../../data/services/session_consolidator.dart';
import '../../data/services/chat_service_provider.dart';
import '../../data/services/chat_backup_service.dart';
import '../../data/services/chat_session_manager.dart';
import '../../data/services/feedback_service.dart';
import '../../data/services/posthog_analytics_service.dart';
import '../../core/network/connectivity_service.dart';
import '../../presentation/screens/app_shell.dart';
import '../../presentation/screens/auth/login_screen.dart';
import '../../presentation/screens/briefing/briefing_screen.dart';
import '../../presentation/screens/chat/chat_screen.dart';
import '../../presentation/screens/get_better/get_better_screen.dart';
import '../../presentation/screens/home/home_screen.dart';
import '../../presentation/screens/onboarding/onboarding_screen.dart';
import '../../presentation/screens/reminders/reminders_screen.dart';
import '../../presentation/screens/settings/settings_screen.dart';
import '../../presentation/screens/subscription/paywall_screen.dart';
import '../../presentation/viewmodels/auth_viewmodel.dart';
import '../../presentation/viewmodels/briefing_viewmodel.dart';
import '../../presentation/viewmodels/text_chat_viewmodel.dart';
GoRouter buildRouter(
  AuthViewModel authViewModel,
  PostHogAnalyticsService postHogAnalyticsService,
) {
  final rootRouteObserver = AppRouteObserver(postHogAnalyticsService: postHogAnalyticsService);
  final shellRouteObserver = AppRouteObserver(postHogAnalyticsService: postHogAnalyticsService);

  return GoRouter(
    initialLocation: '/home',
    observers: [rootRouteObserver],
    refreshListenable: authViewModel,
    redirect: (context, state) {
      final auth = context.read<AuthViewModel>();
      final isReady =
          auth.state != ViewState.idle && auth.state != ViewState.loading;
      final isLoggedIn = auth.isAuthenticated;
      final needsOnboarding = auth.needsOnboarding;
      final location = state.matchedLocation;

      AppLogger.info(
        'Router redirect: location=$location authState=${auth.state} '
        'isReady=$isReady isLoggedIn=$isLoggedIn needsOnboarding=$needsOnboarding',
        tag: 'Router',
      );

      if (!isReady) return null;

      final isOnLogin = location == '/login';
      final isOnOnboarding = location == '/onboarding';

      // If not authenticated, redirect to login.
      if (!isLoggedIn && !isOnLogin) {
        AppLogger.info('Router: -> /login (not authenticated)', tag: 'Router');
        return '/login';
      }

      // Authenticated and left login - route to onboarding or home.
      if (isLoggedIn && isOnLogin) {
        final dest = needsOnboarding ? '/onboarding' : '/home';
        AppLogger.info('Router: -> $dest (authenticated, leaving login)', tag: 'Router');
        return dest;
      }

      // Authenticated but hasn't completed onboarding, enforce the flow.
      if (isLoggedIn && needsOnboarding && !isOnOnboarding) {
        AppLogger.info('Router: -> /onboarding (onboarding incomplete)', tag: 'Router');
        return '/onboarding';
      }

      // If onboarding is complete, then /onboarding is no longer valid.
      if (isLoggedIn && !needsOnboarding && isOnOnboarding) {
        AppLogger.info('Router: -> /home (onboarding already complete)', tag: 'Router');
        return '/home';
      }

      return null;
    },
    routes: [
      GoRoute(
        path: '/login',
        name: 'Login',
        builder: (_, _) => const LoginScreen(),
      ),
      GoRoute(
        path: '/onboarding',
        name: 'Onboarding',
        builder: (_, _) => const OnboardingScreen(),
      ),

      // Shell: single Home surface — provides the ambient background + analytics
      // observer. Notification deep links still open agent threads via the
      // top-level '/agents/:agentId' route below.
      ShellRoute(
        observers: [shellRouteObserver],
        builder: (context, state, child) => AppShell(child: child),
        routes: [
          GoRoute(
            path: '/home',
            name: 'Home',
            builder: (_, _) => const HomeScreen(),
          ),
        ],
      ),

      // Full-screen: Buddy text chat — opened from the drawer
      // sessionId == 'new' means create a fresh session
      GoRoute(
        path: '/chat/:sessionId',
        name: 'Chat',
        pageBuilder: (context, state) {
          final raw = state.pathParameters['sessionId']!;
          final existingId = raw == 'new' ? null : raw;
          return _slidePage(
            state,
            ChangeNotifierProvider(
              create: (_) => TextChatViewModel(
                initialSessionId: existingId,
                backendService: context.read<ChatServiceProvider>(),
                chatRepository: context.read<ChatRepository>(),
                chatBackupService: context.read<ChatBackupService>(),
                feedbackService: context.read<FeedbackService>(),
                connectivityService: context.read<ConnectivityService>(),
                chatSessionManager: context.read<ChatSessionManager>(),
                postHogAnalyticsService: context.read<PostHogAnalyticsService>(),
                suggestionPillsRepository: context.read<AgentSuggestionPillsRepository>(),
                buddyPillsRefresher: context.read<BuddyPillsRefresher>(),
                sessionConsolidator: context.read<SessionConsolidator>(),
              ),
              child: const ChatScreen(),
            ),
          );
        },
      ),

      GoRoute(
        path: '/settings',
        name: 'Settings',
        pageBuilder: (context, state) =>
            _slidePage(state, const SettingsScreen()),
      ),
      GoRoute(
        path: '/paywall',
        name: 'Paywall',
        pageBuilder: (context, state) =>
            _slidePage(state, const PaywallScreen()),
      ),
      GoRoute(
        path: '/reminders',
        name: 'Reminders',
        pageBuilder: (context, state) =>
            _slidePage(state, const RemindersScreen()),
      ),
      GoRoute(
        path: '/get-better',
        name: 'Get Better',
        pageBuilder: (context, state) =>
            _slidePage(state, const GetBetterScreen()),
      ),

      // Full-screen: Daily briefing - opened from the drawer or a briefing push.
      // Carries BOTH the briefing VM (the news) and a scoped chat VM (the embedded
      // in-place chat about the news), mirroring how the /chat route wires up
      // TextChatViewModel above.
      GoRoute(
        path: '/briefing',
        name: 'Briefing',
        pageBuilder: (context, state) => _slidePage(
          state,
          MultiProvider(
            providers: [
              ChangeNotifierProvider(
                create: (_) => BriefingViewModel(
                  backendApiService: context.read<BackendApiService>(),
                  postHogAnalyticsService: context.read<PostHogAnalyticsService>(),
                ),
              ),
              ChangeNotifierProvider(
                create: (_) => TextChatViewModel(
                  backendService: context.read<ChatServiceProvider>(),
                  chatRepository: context.read<ChatRepository>(),
                  chatBackupService: context.read<ChatBackupService>(),
                  feedbackService: context.read<FeedbackService>(),
                  connectivityService: context.read<ConnectivityService>(),
                  chatSessionManager: context.read<ChatSessionManager>(),
                  postHogAnalyticsService: context.read<PostHogAnalyticsService>(),
                  suggestionPillsRepository:
                      context.read<AgentSuggestionPillsRepository>(),
                  buddyPillsRefresher: context.read<BuddyPillsRefresher>(),
                  sessionConsolidator: context.read<SessionConsolidator>(),
                ),
              ),
            ],
            child: const BriefingScreen(),
          ),
        ),
      ),
    ],
  );
}

/// iOS-style right-to-left slide transition for full-screen routes.
CustomTransitionPage<void> _slidePage(GoRouterState state, Widget child) {
  return CustomTransitionPage<void>(
    key: state.pageKey,
    name: state.name,
    child: child,
    transitionsBuilder: (context, animation, secondaryAnimation, child) {
      const begin = Offset(1.0, 0.0);
      const end = Offset.zero;
      final tween = Tween(begin: begin, end: end).chain(
        CurveTween(curve: Curves.easeInOutCubic),
      );
      return SlideTransition(
        position: animation.drive(tween),
        child: child,
      );
    },
    transitionDuration: const Duration(milliseconds: 300),
  );
}
