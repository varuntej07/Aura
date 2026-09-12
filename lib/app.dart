import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:go_router/go_router.dart';
import 'package:provider/provider.dart';

import 'core/analytics/funnel_events.dart';
import 'core/logging/app_logger.dart';
import 'core/router/router.dart';
import 'core/theme/app_theme.dart';
import 'data/services/app_update_service.dart';
import 'data/services/posthog_analytics_service.dart';
import 'data/services/startup_diagnostics_service.dart';
import 'presentation/viewmodels/auth_viewmodel.dart';

class AuraApp extends StatefulWidget {
  const AuraApp({super.key});

  @override
  State<AuraApp> createState() => _AuraAppState();
}

class _AuraAppState extends State<AuraApp> with WidgetsBindingObserver {
  late final GoRouter _router;
  late final AppUpdateService _appUpdateService;
  final GlobalKey<ScaffoldMessengerState> _scaffoldMessengerKey =
      GlobalKey<ScaffoldMessengerState>();

  bool _firstFrameRendered = false;
  bool _updateCheckInProgress = false;
  bool _updateFlowInProgress = false;
  bool _updatePromptVisible = false;
  bool _restartPromptVisible = false;
  int? _availableVersionTracked;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    _appUpdateService = AppUpdateService(
      onUpdateDownloaded: _showDownloadedUpdate,
      onUpdateFailed: _handleUpdateDownloadFailure,
    );
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
      _firstFrameRendered = true;
      // A rendered frame is the only unambiguous proof that this launch worked.
      // It clears the native boot-stage breadcrumb and resets the consecutive-
      // failure counter; a launch that never gets here leaves its stage on disk
      // for the next launch to report. See StartupDiagnosticsService.
      unawaited(StartupDiagnosticsService.markLaunchSucceeded());
      context.read<AuthViewModel>().initialize();
      unawaited(_checkForUpdate());
    });
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state == AppLifecycleState.resumed) unawaited(_checkForUpdate());
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    _appUpdateService.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return MaterialApp.router(
      title: 'Aura',
      debugShowCheckedModeBanner: false,
      theme: AppTheme.dark,
      scaffoldMessengerKey: _scaffoldMessengerKey,
      routerConfig: _router,
    );
  }

  Future<void> _checkForUpdate() async {
    if (!_firstFrameRendered ||
        !_appUpdateService.isSupported ||
        _updateCheckInProgress ||
        _updateFlowInProgress) {
      return;
    }
    _updateCheckInProgress = true;
    try {
      final status = await _appUpdateService.checkForUpdate();
      if (!mounted) return;
      switch (status.state) {
        case AppUpdateState.none:
          return;
        case AppUpdateState.downloaded:
          _showDownloadedUpdate();
          return;
        case AppUpdateState.available:
          if (_availableVersionTracked != status.availableVersionCode) {
            _availableVersionTracked = status.availableVersionCode;
            _trackUpdateEvent(
              FunnelEvents.mobileUpdateAvailable,
              _statusProperties(status),
            );
          }
          final shouldPrompt = await _appUpdateService.shouldPromptFor(
            status.availableVersionCode,
          );
          if (!mounted || !shouldPrompt) return;
          _showAvailableUpdate(status);
          return;
      }
    } on PlatformException catch (error, stackTrace) {
      AppLogger.info(
        'Google Play update check unavailable',
        tag: 'AppUpdate',
        metadata: {'code': error.code},
      );
      _trackUpdateEvent(FunnelEvents.mobileUpdateFailed, {
        'stage': 'check',
        'code': error.code,
      });
      AppLogger.debug(
        'Update check details: $error\n$stackTrace',
        tag: 'AppUpdate',
      );
    } catch (error, stackTrace) {
      AppLogger.error(
        'Unexpected app update check failure',
        error: error,
        stackTrace: stackTrace,
        tag: 'AppUpdate',
      );
      _trackUpdateEvent(FunnelEvents.mobileUpdateFailed, {
        'stage': 'check',
        'code': 'unexpected',
      });
    } finally {
      _updateCheckInProgress = false;
    }
  }

  void _showAvailableUpdate(AppUpdateStatus status) {
    final messenger = _scaffoldMessengerKey.currentState;
    if (messenger == null || _updatePromptVisible || _restartPromptVisible) {
      return;
    }
    _updatePromptVisible = true;
    _trackUpdateEvent(
      FunnelEvents.mobileUpdatePrompted,
      _statusProperties(status),
    );
    unawaited(
      _appUpdateService
          .recordPromptShown(status.availableVersionCode)
          .catchError((Object error) {
            AppLogger.debug(
              'Could not save app update prompt cooldown: $error',
              tag: 'AppUpdate',
            );
          }),
    );
    final controller = messenger.showSnackBar(
      SnackBar(
        content: const Text('A newer, better Aura is ready.'),
        duration: const Duration(seconds: 12),
        action: SnackBarAction(
          label: 'Update',
          onPressed: () => unawaited(_startFlexibleUpdate()),
        ),
      ),
    );
    unawaited(
      controller.closed.whenComplete(() => _updatePromptVisible = false),
    );
  }

  Future<void> _startFlexibleUpdate() async {
    if (_updateFlowInProgress) return;
    _updateFlowInProgress = true;
    try {
      final result = await _appUpdateService.startFlexibleUpdate();
      if (!mounted) return;
      _trackUpdateEvent(FunnelEvents.mobileUpdatePromptResult, {
        'result': result.name,
      });
      if (result == AppUpdateStartResult.accepted) {
        _scaffoldMessengerKey.currentState?.showSnackBar(
          const SnackBar(
            content: Text('Aura is downloading the update in the background.'),
          ),
        );
      }
    } on PlatformException catch (error) {
      AppLogger.warning(
        'Google Play could not start the app update',
        tag: 'AppUpdate',
        metadata: {'code': error.code},
      );
      _trackUpdateEvent(FunnelEvents.mobileUpdateFailed, {
        'stage': 'start',
        'code': error.code,
      });
      _showUpdateError();
    } finally {
      _updateFlowInProgress = false;
    }
  }

  void _showDownloadedUpdate() {
    if (!mounted || !_firstFrameRendered || _restartPromptVisible) return;
    final messenger = _scaffoldMessengerKey.currentState;
    if (messenger == null) return;
    _restartPromptVisible = true;
    _trackUpdateEvent(FunnelEvents.mobileUpdateDownloaded);
    messenger.hideCurrentSnackBar();
    final controller = messenger.showSnackBar(
      SnackBar(
        content: const Text("Aura's update is ready to install."),
        duration: const Duration(days: 1),
        action: SnackBarAction(
          label: 'Restart',
          onPressed: () => unawaited(_completeFlexibleUpdate()),
        ),
      ),
    );
    unawaited(
      controller.closed.whenComplete(() => _restartPromptVisible = false),
    );
  }

  Future<void> _completeFlexibleUpdate() async {
    _trackUpdateEvent(FunnelEvents.mobileUpdateInstallStarted);
    try {
      await _appUpdateService.completeFlexibleUpdate();
    } on PlatformException catch (error) {
      AppLogger.warning(
        'Google Play could not install the downloaded app update',
        tag: 'AppUpdate',
        metadata: {'code': error.code},
      );
      _trackUpdateEvent(FunnelEvents.mobileUpdateFailed, {
        'stage': 'install',
        'code': error.code,
      });
      _showUpdateError();
    }
  }

  void _handleUpdateDownloadFailure(int? errorCode) {
    AppLogger.warning(
      'Google Play app update download failed',
      tag: 'AppUpdate',
      metadata: {'code': errorCode ?? 'unknown'},
    );
    _trackUpdateEvent(FunnelEvents.mobileUpdateFailed, {
      'stage': 'download',
      'code': errorCode ?? 'unknown',
    });
  }

  void _showUpdateError() {
    if (!mounted) return;
    _scaffoldMessengerKey.currentState?.showSnackBar(
      const SnackBar(
        content: Text(
          'The update could not start. You can still update from Google Play.',
        ),
      ),
    );
  }

  Map<String, Object> _statusProperties(AppUpdateStatus status) => {
    if (status.availableVersionCode != null)
      'available_version_code': status.availableVersionCode!,
    if (status.stalenessDays != null) 'staleness_days': status.stalenessDays!,
    'priority': status.priority,
  };

  void _trackUpdateEvent(String event, [Map<String, Object>? properties]) {
    if (!mounted) return;
    unawaited(
      context
          .read<PostHogAnalyticsService>()
          .trackEvent(event, properties: properties)
          .catchError((Object error) {
            AppLogger.debug(
              'Could not record app update analytics: $error',
              tag: 'AppUpdate',
            );
          }),
    );
  }
}
