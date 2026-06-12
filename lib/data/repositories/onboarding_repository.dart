import 'dart:async';

import '../services/backend_api_service.dart';
import '../services/firestore_service.dart';
import '../services/posthog_analytics_service.dart';
import '../../core/constants/app_constants.dart';
import '../../core/logging/app_logger.dart';

class OnboardingRepository {
  final FirestoreService _firestoreService;
  final PostHogAnalyticsService _postHogAnalyticsService;
  final BackendApiService _backendApiService;

  OnboardingRepository({
    required FirestoreService firestoreService,
    required PostHogAnalyticsService postHogAnalyticsService,
    required BackendApiService backendApiService,
  })  : _firestoreService = firestoreService,
        _postHogAnalyticsService = postHogAnalyticsService,
        _backendApiService = backendApiService;

  /// Writes the onboarding result atomically. Called once at the end of the
  /// consent screen. On success the caller should update AuthViewModel in
  /// memory so the router redirect fires without a Firestore round-trip.
  ///
  /// The users/{uid} doc is the source of truth for the relevance signals
  /// (gender, declared interests, locale, language). After it's written, the
  /// declared interests are also seeded into UserAura on the server (consent-gated
  /// there) so the signal engine has a day-one starting direction. That seed is
  /// best-effort: a failure never blocks onboarding, since the allow-list reads
  /// onboarding_interests off the user doc directly.
  Future<bool> saveOnboardingResult({
    required String uid,
    required String dateOfBirth,
    required bool auraConsentGranted,
    required String gender,
    required List<String> interestSlugs,
    required String locale,
    required String language,
  }) async {
    final result = await _firestoreService.updateDocument(
      AppConstants.usersCollection,
      uid,
      {
        'onboarding_complete': true,
        'date_of_birth': dateOfBirth,
        'aura_consent_granted': auraConsentGranted,
        'aura_consent_timestamp': DateTime.now().toUtc().toIso8601String(),
        'gender': gender,
        'onboarding_interests': interestSlugs,
        'locale': locale,
        'language': language,
      },
    );

    return result.when(
      success: (_) {
        AppLogger.info(
          'Onboarding complete: uid=$uid consent=$auraConsentGranted '
          'interests=${interestSlugs.length}',
          tag: 'OnboardingRepository',
        );
        unawaited(_postHogAnalyticsService.trackEvent(
          'onboarding_completed',
          properties: {
            'aura_consent_granted': auraConsentGranted,
            'interest_count': interestSlugs.length,
            'gender': gender,
            'locale': locale,
            'language': language,
          },
        ));
        // Seed UserAura on the server (best-effort; the user doc already holds the
        // declared list the allow-list reads).
        unawaited(_seedServerInterests(interestSlugs));
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

  Future<void> _seedServerInterests(List<String> interestSlugs) async {
    if (interestSlugs.isEmpty) return;
    final result = await _backendApiService.seedOnboardingInterests(interestSlugs);
    result.when(
      success: (_) {},
      failure: (error) => AppLogger.warning(
        'Failed to seed onboarding interests on server (non-blocking)',
        tag: 'OnboardingRepository',
        metadata: {'error': error.message},
      ),
    );
  }
}
