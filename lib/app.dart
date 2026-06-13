import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';
import 'package:provider/provider.dart';

import 'core/router/router.dart';
import 'core/theme/app_theme.dart';
import 'data/services/posthog_analytics_service.dart';
import 'presentation/viewmodels/auth_viewmodel.dart';

class AuraApp extends StatefulWidget {
  const AuraApp({super.key});

  @override
  State<AuraApp> createState() => _AuraAppState();
}

class _AuraAppState extends State<AuraApp> {
  late final GoRouter _router;

  @override
  void initState() {
    super.initState();
    // context.read is safe in initState — widget is already in the tree.
    _router = buildRouter(
      context.read<AuthViewModel>(),
      context.read<PostHogAnalyticsService>(),
    );
    // Defer initialize() to after the first frame so notifyListeners() doesn't
    // fire while the widget tree is still being mounted (setState-during-build).
    WidgetsBinding.instance.addPostFrameCallback((_) {
      context.read<AuthViewModel>().initialize();
    });
  }

  @override
  Widget build(BuildContext context) {
    return MaterialApp.router(
      title: 'Aura',
      debugShowCheckedModeBanner: false,
      theme: AppTheme.dark,
      routerConfig: _router,
    );
  }
}
