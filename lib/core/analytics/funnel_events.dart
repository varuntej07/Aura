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

  // Read-path terminal. A "read" notification opens the article in an in-app
  // browser instead of chat, so `actionAfterNotification` never fires for it.
  // `contentOpened` is the read-path conversion, fired when the browser launches.
  static const String contentOpened = 'content_opened';

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

  // Thread (curiosity follow-up) funnel. Mirrors the signal funnel; step 2
  // reuses `notificationTapped` filtered by `originThreadEngine`. `threadReply`
  // (the action step) fires server-side for a silent shade reply and
  // client-side for an in-chat reply, so both count toward the same conversion.
  static const String threadFollowUpSent = 'thread_followup_sent';
  static const String threadSessionFromNotification = 'thread_session_from_notification';
  static const String threadReply = 'thread_reply';

  // Join key for the thread funnel.
  static const String propThreadId = 'thread_id';

  // Origin value identifying curiosity follow-up notifications.
  static const String originThreadEngine = 'thread_engine';

  // Icebreaker (life-aware opener) funnel. Mirrors the signal/thread funnels;
  // step 2 reuses `notificationTapped` filtered by `originIcebreaker`.
  // `icebreakerReply` (the action step) fires client-side for an in-chat reply.
  static const String icebreakerSent = 'icebreaker_sent';
  static const String icebreakerSessionFromNotification =
      'icebreaker_session_from_notification';
  static const String icebreakerReply = 'icebreaker_reply';

  // Origin value identifying icebreaker notifications.
  static const String originIcebreaker = 'icebreaker';

  // Daily Briefing funnel. The tap opens the briefing SCREEN, then a FAB opens chat
  static const String briefingSent = 'daily_briefing_sent';
  static const String briefingOpened = 'briefing_opened';
  static const String briefingChatStarted = 'briefing_chat_started';

  // Origin value identifying daily-briefing notifications.
  static const String originBriefing = 'daily_briefing';

  // On-demand "Catch me up on the world" snapshot.
  static const String worldBriefingFetched = 'world_briefing_fetched';

  // Most steps fire client-side from the native keyboard / in-app onboarding; 
  // `keyboardDraftRequested` also fires server-side from /keyboard/draft. 
  // Properties carry only the action + host app, never the user's typed content.
  static const String keyboardEnabled = 'keyboard_enabled';
  static const String keyboardFullAccessGranted = 'keyboard_full_access_granted';
  static const String keyboardDraftRequested = 'keyboard_draft_requested';
  static const String keyboardSuggestionInserted = 'keyboard_suggestion_inserted';
  static const String keyboardLimitHit = 'keyboard_limit_hit';

  static const String propKeyboardAction = 'action';
  static const String propKeyboardHostApp = 'host_app';

  // Field-type breakdown dimension stamped onto `keyboardDraftRequested`
  // (text | email | url | number | phone | datetime | password). Breakdown only,
  // never the user's typed content.
  static const String propKeyboardFieldType = 'field_type';

  // Password helper + in-keyboard voice, fired client-side from the native
  // keyboard. Both are content-free: the generated password is never sent
  // anywhere, and the voice-started event carries no transcript or field content.
  static const String keyboardPasswordGenerated = 'keyboard_password_generated';
  static const String keyboardVoiceStarted = 'keyboard_voice_started';

  // Desktop client (Aura-Desktop) product events. Mirrors the Python block of
  // the same name; the desktop repo keeps its own typed list in
  // src/lib/analyticsEvents.ts. All client-side and content-free (durations,
  // counts, enum outcomes, versions). `desktopHeartbeat` fires every 10
  // minutes while the desktop app runs; `clientLog` is the desktop twin of
  // app_logger's remote WARN/ERROR capture.
  static const String desktopClientDesktopAppLaunched = 'desktop_app_launched';
  static const String desktopClientDesktopInstallObserved = 'desktop_install_observed';
  static const String desktopClientDesktopOnboardingStepCompleted = 'desktop_onboarding_step_completed';
  static const String desktopClientDesktopOnboardingCompleted = 'desktop_onboarding_completed';
  static const String desktopClientDesktopTelemetryConsentAccepted = 'desktop_telemetry_consent_accepted';
  static const String desktopClientDesktopOnboardingAuthPathSelected = 'desktop_onboarding_auth_path_selected';
  static const String desktopClientDesktopPrivacySetupSaved = 'desktop_privacy_setup_saved';
  static const String desktopClientDesktopHotkeyTestPassed = 'desktop_hotkey_test_passed';
  static const String desktopClientDesktopHotkeyTestSkipped = 'desktop_hotkey_test_skipped';
  static const String desktopClientDesktopHotkeyTourCompleted = 'desktop_hotkey_tour_completed';
  static const String desktopClientDesktopAgentDemoStarted = 'desktop_agent_demo_started';
  static const String desktopClientDesktopAgentDemoFinished = 'desktop_agent_demo_finished';
  static const String desktopClientDesktopAgentDemoTimedOut = 'desktop_agent_demo_timed_out';
  static const String desktopClientDesktopSignInStarted = 'desktop_sign_in_started';
  static const String desktopClientDesktopSignInCompleted = 'desktop_sign_in_completed';
  static const String desktopClientDesktopSignInFailed = 'desktop_sign_in_failed';
  static const String desktopClientDesktopSignedOut = 'desktop_signed_out';
  static const String desktopClientWebAuthStarted = 'web_auth_started';
  static const String desktopClientWebAuthCompleted = 'web_auth_completed';
  static const String desktopClientWebAuthFailed = 'web_auth_failed';
  static const String desktopClientWebAuthExpired = 'web_auth_expired';
  static const String desktopClientWebAuthCancelled = 'web_auth_cancelled';
  static const String desktopClientDesktopCheckoutStarted = 'desktop_checkout_started';
  static const String desktopClientDesktopCheckoutUpgraded = 'desktop_checkout_upgraded';
  static const String desktopClientDesktopCheckoutDeadline = 'desktop_checkout_deadline';
  static const String desktopClientDesktopNotificationFetched = 'desktop_notification_fetched';
  static const String desktopClientDesktopNotificationQueued = 'desktop_notification_queued';
  static const String desktopClientDesktopNotificationToastShown = 'desktop_notification_toast_shown';
  static const String desktopClientDesktopNotificationToastDenied = 'desktop_notification_toast_denied';
  static const String desktopClientDesktopNotificationDismissed = 'desktop_notification_dismissed';
  static const String desktopClientDesktopNotificationPageDropped = 'desktop_notification_page_dropped';
  static const String desktopClientVoiceStartFailed = 'voice_start_failed';
  static const String desktopClientChatSessionStarted = 'chat_session_started';
  static const String desktopClientChatMessageSent = 'chat_message_sent';
  static const String desktopClientChatTurnFailed = 'chat_turn_failed';
  static const String desktopClientDraftCardCopied = 'draft_card_copied';
  static const String desktopClientDraftCardDismissed = 'draft_card_dismissed';
  static const String desktopClientCallbackCardShown = 'callback_card_shown';
  static const String desktopClientCallbackCardEngaged10s = 'callback_card_engaged_10s';
  static const String desktopClientCallbackCardDismissed = 'callback_card_dismissed';
  static const String desktopClientCallbackCardToggleOff = 'callback_card_toggle_off';
  static const String desktopClientCallbackChipDeleted = 'callback_chip_deleted';
  static const String desktopClientTurnContextUpload = 'turn_context_upload';
  static const String desktopClientScreenSightToggled = 'screen_sight_toggled';
  static const String desktopClientMeetingAutoSummon = 'meeting_auto_summon';
  static const String desktopClientMeetingTickerDismissed = 'meeting_ticker_dismissed';
  static const String desktopClientMeetingAlertsToggleOff = 'meeting_alerts_toggle_off';
  static const String desktopClientMeetingNotesArmToggled = 'meeting_notes_arm_toggled';
  static const String desktopClientMeetingNotesAutoToggled = 'meeting_notes_auto_toggled';
  static const String desktopClientMeetingCaptureStarted = 'meeting_capture_started';
  static const String desktopClientMeetingCaptureManual = 'meeting_capture_manual';
  static const String desktopClientMeetingCaptureCompleted = 'meeting_capture_completed';
  static const String desktopClientMeetingCaptureFailed = 'meeting_capture_failed';
  static const String desktopClientMeetingCapBlocked = 'meeting_cap_blocked';
  static const String desktopClientMeetingUploadAttempt = 'meeting_upload_attempt';
  static const String desktopClientMeetingNoteCardShown = 'meeting_note_card_shown';
  static const String desktopClientMeetingNoteCardDismissed = 'meeting_note_card_dismissed';
  static const String desktopClientMeetingNoteCardToggleOff = 'meeting_note_card_toggle_off';
  static const String desktopClientGuideSession = 'guide_session';
  static const String desktopClientGuideCompleted = 'guide_completed';
  static const String desktopClientGuideAbandoned = 'guide_abandoned';
  static const String desktopClientGuideStepsReceived = 'guide_steps_received';
  static const String desktopClientGuideAutoFramesSent = 'guide_auto_frames_sent';
  static const String desktopClientGuideAgentTimeouts = 'guide_agent_timeouts';
  static const String desktopClientInterviewCompanionSessionStarted = 'interview_companion_session_started';
  static const String desktopClientInterviewCompanionSessionEnded = 'interview_companion_session_ended';
  static const String desktopClientInterviewCompanionTurnLatency = 'interview_companion_turn_latency';
  static const String desktopClientInterviewCompanionFirstAnswerText = 'interview_companion_first_answer_text';
  static const String desktopClientInterviewCompanionAnswerCompleted = 'interview_companion_answer_completed';
  static const String desktopClientInterviewCompanionQuestionDecision = 'interview_companion_question_decision';
  static const String desktopClientInterviewCompanionResumeAttached = 'interview_companion_resume_attached';
  static const String desktopClientInterviewCompanionCredentialRotation = 'interview_companion_credential_rotation';
  static const String desktopClientInterviewCompanionScreenSight = 'interview_companion_screen_sight';
  static const String desktopClientInterviewCompanionReflection = 'interview_companion_reflection';
  static const String desktopClientInterviewCompanionRecovery = 'interview_companion_recovery';
  static const String desktopClientInterviewCompanionError = 'interview_companion_error';
  static const String desktopClientDesktopDictationShareDrain = 'desktop_dictation_share_drain';
  static const String desktopClientDictationHoldCompleted = 'dictation_hold_completed';
  static const String desktopClientDesktopUpdateAvailable = 'desktop_update_available';
  static const String desktopClientDesktopUpdateCheckFailed = 'desktop_update_check_failed';
  static const String desktopClientDesktopUpdateInstallStarted = 'desktop_update_install_started';
  static const String desktopClientDesktopUpdateInstallResult = 'desktop_update_install_result';
  static const String desktopClientDesktopPermissionResult = 'desktop_permission_result';
  static const String desktopClientFeedbackSubmitted = 'feedback_submitted';
  static const String desktopClientDesktopHeartbeat = 'desktop_heartbeat';
  static const String desktopClientDesktopStartupDiagnosticsSent = 'desktop_startup_diagnostics_sent';
  static const String desktopClientClientLog = 'client_log';

  // Home deck. Every one of these carries propDeckCardKind: without the kind
  // there is no way to tell which catalog entries earn their slot, which is the
  // whole question the deck exists to answer.
  static const String deckCardThrown = 'deck_card_thrown';
  static const String deckCardOpened = 'deck_card_opened';
  static const String deckCardConfirmed = 'deck_card_confirmed';
  static const String deckCardFailed = 'deck_card_failed';
  static const String deckCardDismissed = 'deck_card_dismissed';
  static const String propDeckCardKind = 'deck_card_kind';

  // Breakdown keys stamped by the desktop client.
  static const String propDesktopWindowLabel = 'window_label';
  static const String propDesktopInstallId = 'install_id';
  static const String propDesktopLastExitKind = 'last_exit_kind';
  static const String propDesktopConsecutiveFailedLaunches = 'consecutive_failed_launches';
}
