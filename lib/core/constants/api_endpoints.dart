import '../config/environment.dart';

class ApiEndpoints {
  ApiEndpoints._();

  static String get baseUrl => Environment.current.apiBaseUrl;

  // REST endpoints
  static String get chat => '$baseUrl/chat';
  static String get memories => '$baseUrl/memories';
  static String get reminders => '$baseUrl/reminders';

  // Device / push notification token registration
  static String get deviceRegister => '$baseUrl/devices/register';

  // Voice session returns LiveKit room token for the Flutter client
  static String get voiceToken => '$baseUrl/voice/token';

  // Subscription state + web checkout. Relative paths on purpose: these are
  // passed to ApiClient, which prepends the base URL itself (unlike the full
  // URLs above, which belong to callers that build their own requests).
  static const String entitlement = '/entitlement';
  static const String billingCheckout = '/billing/checkout';
}
