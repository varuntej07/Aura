import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';
import 'package:provider/provider.dart';

import '../../data/repositories/agent_suggestion_pills_repository.dart';
import '../../data/repositories/chat_repository.dart';
import '../../data/services/backend_api_service.dart';
import '../../data/services/chat_backup_service.dart';
import '../../data/services/chat_session_manager.dart';
import '../../data/services/feedback_service.dart';
import '../../core/network/connectivity_service.dart';
import '../../presentation/screens/app_shell.dart';
import '../../presentation/screens/agents/agents_screen.dart';
import '../../presentation/screens/agents/agent_thread_screen.dart';
import '../../presentation/screens/auth/login_screen.dart';
import '../../presentation/screens/chat/chat_screen.dart';
import '../../presentation/screens/home/home_screen.dart';
import '../../presentation/screens/nutrition/nutrition_scan_screen.dart';
import '../../presentation/screens/reminders/reminders_screen.dart';
import '../../presentation/screens/settings/settings_screen.dart';
import '../../presentation/screens/subscription/paywall_screen.dart';
import '../../presentation/viewmodels/agent_viewmodel.dart';
import '../../presentation/viewmodels/auth_viewmodel.dart';
import '../../presentation/viewmodels/text_chat_viewmodel.dart';
import '../../presentation/viewmodels/view_state.dart';

GoRouter buildRouter(AuthViewModel authViewModel) {
  return GoRouter(
    initialLocation: '/home',
    refreshListenable: authViewModel,
    redirect: (context, state) {
      final auth = context.read<AuthViewModel>();
      final isReady =
          auth.state != ViewState.idle && auth.state != ViewState.loading;

      if (!isReady) return null;

      final isLoggedIn = auth.isAuthenticated;
      final isOnLogin = state.matchedLocation == '/login';

      if (!isLoggedIn && !isOnLogin) return '/login';
      if (isLoggedIn && isOnLogin) return '/home';
      return null;
    },
    routes: [
      GoRoute(
        path: '/login',
        builder: (_, __) => const LoginScreen(),
      ),

      // Shell: Home + Agents tabs — bottom nav persists across both
      ShellRoute(
        builder: (context, state, child) => AppShell(child: child),
        routes: [
          GoRoute(
            path: '/home',
            builder: (_, __) => const HomeScreen(),
          ),
          GoRoute(
            path: '/agents',
            builder: (_, __) => const AgentsScreen(),
          ),
        ],
      ),

      // Full-screen: Agent chat thread — slides over the shell, no bottom nav
      GoRoute(
        path: '/agents/:agentId',
        pageBuilder: (context, state) {
          final agentId = state.pathParameters['agentId']!;
          final chatOpener = state.extra as String?;
          return _slidePage(
            state,
            ChangeNotifierProvider(
              create: (_) => AgentViewModel(
                agentId: agentId,
                backendService: context.read<BackendApiService>(),
                chatRepository: context.read<ChatRepository>(),
                chatBackupService: context.read<ChatBackupService>(),
                feedbackService: context.read<FeedbackService>(),
                connectivityService: context.read<ConnectivityService>(),
                chatSessionManager: context.read<ChatSessionManager>(),
                suggestionPillsRepository:
                    context.read<AgentSuggestionPillsRepository>(),
              ),
              child: AgentThreadScreen(agentId: agentId, chatOpener: chatOpener),
            ),
          );
        },
      ),

      // Full-screen: Buddy text chat — opened from the drawer
      // sessionId == 'new' means create a fresh session
      GoRoute(
        path: '/chat/:sessionId',
        pageBuilder: (context, state) {
          final raw = state.pathParameters['sessionId']!;
          final existingId = raw == 'new' ? null : raw;
          return _slidePage(
            state,
            ChangeNotifierProvider(
              create: (_) => TextChatViewModel(
                initialSessionId: existingId,
                backendService: context.read<BackendApiService>(),
                chatRepository: context.read<ChatRepository>(),
                chatBackupService: context.read<ChatBackupService>(),
                feedbackService: context.read<FeedbackService>(),
                connectivityService: context.read<ConnectivityService>(),
                chatSessionManager: context.read<ChatSessionManager>(),
              ),
              child: const ChatScreen(),
            ),
          );
        },
      ),

      GoRoute(
        path: '/settings',
        pageBuilder: (context, state) =>
            _slidePage(state, const SettingsScreen()),
      ),
      GoRoute(
        path: '/paywall',
        pageBuilder: (context, state) =>
            _slidePage(state, const PaywallScreen()),
      ),
      GoRoute(
        path: '/nutrition',
        pageBuilder: (context, state) =>
            _slidePage(state, const NutritionScanScreen()),
      ),
      GoRoute(
        path: '/reminders',
        pageBuilder: (context, state) =>
            _slidePage(state, const RemindersScreen()),
      ),
    ],
  );
}

/// iOS-style right-to-left slide transition for full-screen routes.
CustomTransitionPage<void> _slidePage(GoRouterState state, Widget child) {
  return CustomTransitionPage<void>(
    key: state.pageKey,
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
