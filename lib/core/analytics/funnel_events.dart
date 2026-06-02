/// Single source of truth (client side) for the re-engagement notification
/// funnel contract. Mirrors `backend/src/services/analytics/funnel_events.py`.
///
/// PostHog only joins the four funnel steps if both sides use identical strings.
/// `backend/tests/test_funnel_event_contract.py` reads this file and fails CI if any value drifts 
/// from the Python constants, so a rename can never silently flatten the funnel.
class FunnelEvents {
  FunnelEvents._();

  // Funnel event names (the four ordered steps). `notificationTapped` reuses
  // the app's existing generic tap event; the funnel filters it to signal-engine
  // taps via `propNotificationOrigin`.
  static const String notificationSent = 'signal_notification_sent';
  static const String notificationTapped = 'notification_tapped';
  static const String sessionFromNotification = 'signal_session_from_notification';
  static const String actionAfterNotification = 'signal_action_after_notification';

  // Shared property keys (the join keys across server and client). These match
  // the FCM data-payload keys so a notification's analytics and push payload agree.
  static const String propNotificationId = 'notification_id';
  static const String propContentId = 'content_id';
  static const String propCategory = 'category';
  static const String propNotificationOrigin = 'notification_origin';

  // Firebase uid stamped onto the client tap event. The server keys its
  // `signal_notification_sent` on the same uid (as PostHog distinct_id), so
  // carrying it here lets the funnel join survive a cold-launch tap that fires
  // before `identifyUser(uid)` lands.
  static const String propFirebaseUid = 'firebase_uid';

  // Origin value identifying signal-engine notifications.
  static const String originSignalEngine = 'signal_engine';
}
