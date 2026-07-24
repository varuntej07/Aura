class AppConstants {
  AppConstants._();

  // Timeouts
  static const apiConnectTimeout = Duration(seconds: 10);
  static const apiReadTimeout = Duration(seconds: 10);
  // Claude multi-turn with tool use + exponential backoff can take 15-30s under load.
  static const chatRequestTimeout = Duration(seconds: 45);
  static const chatStreamConnectTimeout = Duration(seconds: 10);
  static const chatStreamIdleTimeout = Duration(seconds: 90);
  // Also covers desktop pairing (devices/pair/start, devices/unlink): both can
  // hit a cold Cloud Run instance (min-instances=0) well past the default read timeout.
  static const apiWriteTimeout = Duration(seconds: 30);
  static const webSocketPingInterval = Duration(seconds: 15);
  static const webSocketReconnectDelay = Duration(seconds: 3);
  static const voiceSessionTimeout = Duration(minutes: 8);
  static const silenceDetectionTimeout = Duration(seconds: 3);

  // Retries
  static const maxApiRetries = 3;
  static const retryBaseDelay = Duration(seconds: 1);

  // External links
  // Marketing site that hosts the Aura-Desktop (Windows) download. The page
  // resolves "latest" from GitHub Releases at request time, so the mobile app
  // never pins a versioned installer URL that would 404 on the next desktop
  // release. See ECOSYSTEM.md (Aura-Web download page).
  static const desktopDownloadUrl = 'https://auravoiceapp.com';

  // UI
  static const animationDuration = Duration(milliseconds: 300);
  static const micButtonSize = 72.0;
  static const maxResponseHistoryItems = 50;

  // Chat history window sent to backend for multi-turn context.
  // 30 messages covers ~15 turns at roughly 3k tokens per request.
  // Matches CHAT_HISTORY_WINDOW in backend/src/config/settings.py.
  static const chatHistoryWindow = 30;

  // Firestore collections
  static const memoriesCollection = 'memories';
  static const remindersCollection = 'reminders';
  static const calendarCacheCollection = 'calendar_cache';
  static const usersCollection = 'users';
  // Root-level collection for explicit, user-submitted feedback. Distinct from `observed_feedback`, 
  // which the backend writes when Buddy notices feedback mid-conversation.
  static const userFeedbackCollection = 'user_feedback';
}
