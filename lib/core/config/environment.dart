import 'package:flutter/foundation.dart';

enum Env { dev, staging, prod }

/// Per-environment configuration.
///
/// Note what is NOT here: the Firebase project. There used to be a
/// `firebaseProjectId` field that was declared, set to a different value per
/// env, and never read by a single line of code — Firebase is configured
/// entirely by `firebase_options.dart` and `google-services.json`. It made
/// switching environments look like it would repoint Firebase, which is
/// precisely the fear that kept this app shipping as a dev build. Removed so
/// the config states only what it actually controls.
class EnvironmentConfig {
  final Env env;
  final String apiBaseUrl;
  final String wsBaseUrl;
  final String googleServerClientId;

  const EnvironmentConfig({
    required this.env,
    required this.apiBaseUrl,
    required this.wsBaseUrl,
    required this.googleServerClientId,
  });
}

class Environment {
  Environment._();

  /// The build mode picks the default, so a forgotten `--dart-define` can never
  /// again ship a production binary that believes it is a dev build.
  ///
  /// It did exactly that: no build command in this repo passed `ENV`, so every
  /// release on the Play Store resolved to `dev`. That silently disabled crash
  /// reporting and Firebase Analytics, and made the app hand every paying-age
  /// user a fabricated Pro entitlement while the backend enforced the real one.
  ///
  /// An explicit `--dart-define=ENV=staging` still wins; debug and profile
  /// builds still default to `dev`, which is what local development wants.
  static const String _envValue = String.fromEnvironment(
    'ENV',
    defaultValue: kReleaseMode ? 'prod' : 'dev',
  );

  // Compile-time overrides for pointing a `flutter run` build at a specific
  // backend (e.g. a Cloud Run candidate revision) without touching prod.
  // Empty unless passed via --dart-define=API_BASE_URL=... / WS_BASE_URL=...
  static const String _apiTestBaseUrl = String.fromEnvironment('API_BASE_URL');
  static const String _wsBaseUrlOverride = String.fromEnvironment('WS_BASE_URL');

  static Env get _env {
    switch (_envValue) {
      case 'prod':
        return Env.prod;
      case 'staging':
        return Env.staging;
      default:
        return Env.dev;
    }
  }

  static EnvironmentConfig get current {
    switch (_env) {
      case Env.prod:
        return const EnvironmentConfig(
          env: Env.prod,
          // Set after first Cloud Run deploy — copy the URL printed by deploy.sh
          apiBaseUrl: 'https://juno-backend-620715294422.us-central1.run.app',
          wsBaseUrl: 'wss://juno-backend-620715294422.us-central1.run.app',
          googleServerClientId: '620715294422-15h8gdqn7ii0b419ksfrf8u7fgghltoi.apps.googleusercontent.com',
        );
      case Env.staging:
        return const EnvironmentConfig(
          env: Env.staging,
          apiBaseUrl: 'https://staging.api.juno-app.com',
          wsBaseUrl: 'wss://staging.api.juno-app.com',
          googleServerClientId: '620715294422-15h8gdqn7ii0b419ksfrf8u7fgghltoi.apps.googleusercontent.com',
        );
      case Env.dev:
        // Points to the deployed GCP backend so flutter run works without a local server.
        // To run against a local uvicorn instead, swap apiBaseUrl/wsBaseUrl with
        // DevTargets.devApiBaseUrl / DevTargets.devWsBaseUrl from dev_targets.dart.
        return EnvironmentConfig(
          env: Env.dev,
          apiBaseUrl: _apiTestBaseUrl.isNotEmpty
              ? _apiTestBaseUrl
              : 'https://juno-backend-620715294422.us-central1.run.app',
          wsBaseUrl: _wsBaseUrlOverride.isNotEmpty
              ? _wsBaseUrlOverride
              : 'wss://juno-backend-620715294422.us-central1.run.app',
          googleServerClientId: '620715294422-15h8gdqn7ii0b419ksfrf8u7fgghltoi.apps.googleusercontent.com',
        );
    }
  }

  static bool get isDev => _env == Env.dev;
  static bool get isProd => _env == Env.prod;

  /// How the binary was compiled, independent of [isDev]/[isProd].
  ///
  /// [Env] comes from `--dart-define=ENV`, which NO build command in this repo
  /// passes, so every shipped Play build reports `dev`. Anything that must be
  /// correct in a released binary (crash reporting, analytics) has to key off
  /// the build mode instead, which the compiler decides and nobody can forget.
  /// This is exposed so a crash report can carry BOTH facts and the discrepancy
  /// is visible in Crashlytics rather than silent.
  static String get buildMode {
    if (kDebugMode) return 'debug';
    if (kProfileMode) return 'profile';
    return 'release';
  }

  /// The `environment` custom key attached to every crash report, e.g.
  /// `dev/release` — which is exactly the mismatch that hid production crashes.
  static String get crashReportingTag => '${current.env.name}/$buildMode';

  static bool get hasConfiguredApi =>
      !current.apiBaseUrl.contains('PLACEHOLDER_');

  static bool get hasConfiguredVoiceGateway =>
      !current.wsBaseUrl.contains('PLACEHOLDER_');
}
