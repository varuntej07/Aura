import '../../../core/logging/app_logger.dart';
import '../notification_service.dart';

/// Windows has no FCM, so the desktop build must never register 
/// device tokens or touch firebase_messaging. AuthViewModel calls initialize(uid) 
/// on every sign-in; this stub keeps that contract as a no-op while 
/// everything else in the auth flow behaves identically to mobile.
class DesktopNotificationServiceStub extends NotificationService {
  DesktopNotificationServiceStub({
    required super.apiClient,
    required super.postHogAnalyticsService,
  });

  @override
  Future<void> initialize(String userId) async {
    AppLogger.info('Notification init skipped on desktop (no FCM)',
        tag: 'DesktopNotifications'
        );
    }
}
