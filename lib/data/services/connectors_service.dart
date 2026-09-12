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

  // Notion
  //
  // Unlike the two above, Notion is authorized in the browser: the backend mints
  // a ten-minute attempt and hands back a URL, and the provider redirects to
  // aura://connectors/complete when the user is done. No token ever reaches the
  // phone, so there is no scope list here.

  /// Starts a browser authorization and returns the URL to open.
  Future<Result<String>> startNotionOAuth() {
    return _apiClient.post(
      '/connectors/oauth/authorize',
      const {'connector': 'notion'},
      (json) => json['authorization_url'] as String? ?? '',
    );
  }

  /// Re-enables a connector whose tokens are still on file. Fails with a 409
  /// carrying `reauthorization_required` when they are not, which is the caller's
  /// signal to send the user through [startNotionOAuth] instead.
  Future<Result<NotionConnectorStatus>> enableNotion() {
    return _apiClient.post(
      '/connectors/notion/enable',
      const {},
      NotionConnectorStatus.fromJson,
    );
  }

  Future<Result<NotionConnectorStatus>> disableNotion() {
    return _apiClient.post(
      '/connectors/notion/disable',
      const {},
      NotionConnectorStatus.fromJson,
    );
  }
}
