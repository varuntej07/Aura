import '../../core/base/safe_change_notifier.dart';
import '../../core/errors/app_exception.dart';
import '../../core/logging/app_logger.dart';
import '../../data/models/connector_models.dart';
import '../../data/services/connectors_service.dart';
import 'view_state.dart';

export 'view_state.dart';

class ConnectorsViewModel extends SafeChangeNotifier {
  final ConnectorsService _connectorService;

  ViewState _state = ViewState.idle;
  GoogleCalendarConnectorStatus _googleCalendar =
      const GoogleCalendarConnectorStatus(
        enabled: false,
        watchActive: false,
        automaticSyncAvailable: false,
        webhookUrlConfigured: false,
        calendarId: 'primary',
        calendarName: 'Primary',
        calendarTimeZone: null,
        connectedAt: null,
        lastSyncedAt: null,
        lastSyncStatus: null,
        watchExpiresAt: null,
        pendingSync: false,
        lastError: null,
      );
  GmailConnectorStatus _gmail = const GmailConnectorStatus(
    enabled: false,
    emailAddress: null,
    connectedAt: null,
    lastError: null,
  );
  AppException? _error;
  bool _isMutating = false;

  ConnectorsViewModel({
    required ConnectorsService connectorService,
  }) : _connectorService = connectorService;

  ViewState get state => _state;
  GoogleCalendarConnectorStatus get googleCalendar => _googleCalendar;
  GmailConnectorStatus get gmail => _gmail;
  AppException? get error => _error;
  bool get isMutating => _isMutating;

  void _setState(ViewState value) {
    _state = value;
    safeNotifyListeners();
  }

  Future<void> load() async {
    _setState(ViewState.loading);
    final result = await _connectorService.fetchConnectors();
    result.when(
      success: (catalog) {
        _googleCalendar = catalog.googleCalendar;
        _gmail = catalog.gmail;
        _error = null;
        _setState(ViewState.loaded);
      },
      failure: (error) {
        _error = error;
        _setState(ViewState.error);
      },
    );
  }

  Future<void> toggleGoogleCalendar(bool enabled) async {
    _isMutating = true;
    safeNotifyListeners();

    final result = enabled
        ? await _connectorService.connectGoogleCalendar()
        : await _connectorService.disconnectGoogleCalendar();

    result.when(
      success: (status) {
        _googleCalendar = status;
        _error = null;
        _state = ViewState.loaded;
      },
      failure: (error) {
        // User backed out of the Google account picker before connecting —
        // that's a normal choice, not an error. Quietly leave the toggle off
        // instead of flashing a red error banner at them.
        if (error.code == ErrorCode.authCancelled) {
          AppLogger.info(
            'Google Calendar connect cancelled by user',
            tag: 'ConnectorsVM',
          );
          _error = null;
          _state = ViewState.loaded;
        } else {
          _error = error;
          _state = ViewState.error;
          AppLogger.error(
            'Google Calendar toggle failed',
            error: error,
            tag: 'ConnectorsVM',
          );
        }
      },
    );

    _isMutating = false;
    safeNotifyListeners();
  }

  Future<void> syncGoogleCalendar() async {
    _isMutating = true;
    safeNotifyListeners();

    final result = await _connectorService.syncGoogleCalendar();
    result.when(
      success: (status) {
        _googleCalendar = status;
        _error = null;
        _state = ViewState.loaded;
      },
      failure: (error) {
        _error = error;
        _state = ViewState.error;
        AppLogger.error(
          'Manual Google Calendar sync failed',
          error: error,
          tag: 'ConnectorsVM',
        );
      },
    );

    _isMutating = false;
    safeNotifyListeners();
  }

  Future<void> toggleGmail(bool enabled) async {
    _isMutating = true;
    safeNotifyListeners();

    final result = enabled
        ? await _connectorService.connectGmail()
        : await _connectorService.disconnectGmail();

    result.when(
      success: (status) {
        _gmail = status;
        _error = null;
        _state = ViewState.loaded;
      },
      failure: (error) {
        _error = error;
        _state = ViewState.error;
        AppLogger.error(
          'Gmail toggle failed',
          error: error,
          tag: 'ConnectorsVM',
        );
      },
    );

    _isMutating = false;
    safeNotifyListeners();
  }

  void clearError() {
    _error = null;
    safeNotifyListeners();
  }
}
