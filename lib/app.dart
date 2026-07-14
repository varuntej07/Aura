import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
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
    // Keep edge-to-edge explicit for older Android versions as well as the
    // SDK 35+ default. Screen content handles system insets with SafeArea or
    // MediaQuery padding, while backgrounds can continue behind the bars.
    SystemChrome.setEnabledSystemUIMode(SystemUiMode.edgeToEdge);

    // Cream theme: dark system-bar icons globally. The AppBar theme covers
    // screens with an AppBar; this default covers the ones that don't.
    SystemChrome.setSystemUIOverlayStyle(
      const SystemUiOverlayStyle(
        statusBarColor: Colors.transparent,
        statusBarIconBrightness: Brightness.dark,
        statusBarBrightness: Brightness.light,
        systemNavigationBarColor: Colors.transparent,
        systemNavigationBarIconBrightness: Brightness.dark,
        systemNavigationBarDividerColor: Colors.transparent,
      ),
    );
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
