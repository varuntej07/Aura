import '../../core/network/api_response.dart';
import '../models/connector_models.dart';
import 'firebase_auth_service.dart';
import '../../core/network/api_client.dart';

class ConnectorsService {
  static const _calendarScope = 'https://www.googleapis.com/auth/calendar.events';
  static const _gmailReadScope = 'https://www.googleapis.com/auth/gmail.readonly';
  static const _gmailSendScope = 'https://www.googleapis.com/auth/gmail.send';

  final ApiClient _apiClient;
  final FirebaseAuthService _authService;

  ConnectorsService({
    required ApiClient apiClient,
    required FirebaseAuthService authService,
  }) : _apiClient = apiClient,
       _authService = authService;

  Future<Result<ConnectorsCatalog>> fetchConnectors() {
    return _apiClient.get('/connectors', ConnectorsCatalog.fromJson);
  }

  // Google Calendar

  Future<Result<GoogleCalendarConnectorStatus>> connectGoogleCalendar() async {
    final authCodeResult = await _authService.requestServerAuthCode(
      const [_calendarScope],
    );

    return authCodeResult.when(
      success: (authCode) {
        return _apiClient.post(
          '/connectors/google-calendar/connect',
          {'server_auth_code': authCode},
          GoogleCalendarConnectorStatus.fromJson,
        );
      },
      failure: (error) => Future.value(Result.failure(error)),
    );
  }

  Future<Result<GoogleCalendarConnectorStatus>> disconnectGoogleCalendar() {
    return _apiClient.post(
      '/connectors/google-calendar/disconnect',
      const {},
      GoogleCalendarConnectorStatus.fromJson,
    );
  }

  Future<Result<GoogleCalendarConnectorStatus>> syncGoogleCalendar() {
    return _apiClient.post(
      '/connectors/google-calendar/sync',
      const {},
      GoogleCalendarConnectorStatus.fromJson,
    );
  }

  // Gmail

  Future<Result<GmailConnectorStatus>> connectGmail() async {
    final authCodeResult = await _authService.requestServerAuthCode(
      const [_gmailReadScope, _gmailSendScope],
    );

    return authCodeResult.when(
      success: (authCode) {
        return _apiClient.post(
          '/connectors/gmail/connect',
          {'server_auth_code': authCode},
          GmailConnectorStatus.fromJson,
        );
      },
      failure: (error) => Future.value(Result.failure(error)),
    );
  }

  Future<Result<GmailConnectorStatus>> disconnectGmail() {
    return _apiClient.post(
      '/connectors/gmail/disconnect',
      const {},
      GmailConnectorStatus.fromJson,
    );
  }
}
