import 'dart:async';

import '../services/firestore_service.dart';
import '../services/posthog_analytics_service.dart';
import '../../core/constants/app_constants.dart';
import '../../core/logging/app_logger.dart';

class OnboardingRepository {
  final FirestoreService _firestoreService;
  final PostHogAnalyticsService _postHogAnalyticsService;

  OnboardingRepository({
    required FirestoreService firestoreService,
    required PostHogAnalyticsService postHogAnalyticsService,
  })  : _firestoreService = firestoreService,
        _postHogAnalyticsService = postHogAnalyticsService;

  /// Writes the onboarding result atomically. Called once at the end of the
  /// consent screen. On success the caller should update AuthViewModel in
  /// memory so the router redirect fires without a Firestore round-trip.
  Future<bool> saveOnboardingResult({
    required String uid,
    required String dateOfBirth,
    required bool auraConsentGranted,
  }) async {
    final result = await _firestoreService.updateDocument(
      AppConstants.usersCollection,
      uid,
      {
        'onboarding_complete': true,
        'date_of_birth': dateOfBirth,
        'aura_consent_granted': auraConsentGranted,
        'aura_consent_timestamp': DateTime.now().toUtc().toIso8601String(),
      },
    );

    return result.when(
      success: (_) {
        AppLogger.info(
          'Onboarding complete: uid=$uid consent=$auraConsentGranted',
          tag: 'OnboardingRepository',
        );
        unawaited(_postHogAnalyticsService.trackEvent(
          'onboarding_completed',
          properties: {'aura_consent_granted': auraConsentGranted},
        ));
        return true;
      },
      failure: (error) {
        AppLogger.error(
          'Failed to save onboarding result',
          error: error,
          tag: 'OnboardingRepository',
        );
        return false;
      },
    );
  }
}
