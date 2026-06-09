import 'dart:async';
import 'dart:io';

import '../../core/constants/app_constants.dart';
import '../../core/logging/app_logger.dart';
import 'firestore_service.dart';
import 'posthog_analytics_service.dart';

/// The single write path for all user feedback.
///
/// Every report — typed feedback from Settings and like/dislike ratings from
/// the voice orb lands in the root `app_feedback/{timestamp}_{uid}` collection and 
/// fires a PostHog `feedback_submitted` event.
class AppFeedbackService {
  final FirestoreService _firestoreService;
  final PostHogAnalyticsService _postHogAnalyticsService;

  AppFeedbackService({
    required FirestoreService firestoreService,
    required PostHogAnalyticsService postHogAnalyticsService,
  })  : _firestoreService = firestoreService,
        _postHogAnalyticsService = postHogAnalyticsService;

  /// Writes one feedback document and fires the analytics event.
  ///
  /// [extraFields] are merged into the Firestore document (e.g. the voice
  /// rating, reasons, duration). [extraEventProperties] are merged into the
  /// PostHog event alongside `category`.
  ///
  /// Returns null on success, or a user-facing error message on failure.
  Future<String?> submit({
    required String uid,
    required String category,
    String text = '',
    Map<String, dynamic> extraFields = const {},
    Map<String, Object> extraEventProperties = const {},
  }) async {
    unawaited(_postHogAnalyticsService.trackEvent(
      'feedback_submitted',
      properties: {'category': category, ...extraEventProperties},
    ));

    // Timestamp prefix keeps the console's lexicographic doc ordering
    // chronological; the uid suffix prevents collisions across users.
    final timestamp = DateTime.now().toUtc().millisecondsSinceEpoch.toString();
    final docId = '${timestamp}_$uid';
    final result = await _firestoreService.setDocument<Map<String, dynamic>>(
      AppConstants.appFeedbackCollection,
      docId,
      {
        'uid': uid,
        'text': text.trim(),
        'category': category,
        'created_at': DateTime.now().toUtc().toIso8601String(),
        'app_version': '1.0.0',
        'platform': Platform.isIOS ? 'ios' : 'android',
        ...extraFields,
      },
      (json) => json,
      merge: false,
    );

    return result.when(
      success: (_) {
        AppLogger.info('Feedback submitted ($category)', tag: 'AppFeedbackService');
        return null;
      },
      failure: (error) {
        AppLogger.error('Feedback submit failed',
            error: error, tag: 'AppFeedbackService');
        return "Couldn't send that just now. Check your connection and try again.";
      },
    );
  }
}
