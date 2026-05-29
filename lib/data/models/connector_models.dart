class GoogleCalendarConnectorStatus {
  final bool enabled;
  final bool watchActive;
  final bool automaticSyncAvailable;
  final bool webhookUrlConfigured;
  final String calendarId;
  final String calendarName;
  final String? calendarTimeZone;
  final DateTime? connectedAt;
  final DateTime? lastSyncedAt;
  final String? lastSyncStatus;
  final DateTime? watchExpiresAt;
  final bool pendingSync;
  final String? lastError;

  const GoogleCalendarConnectorStatus({
    required this.enabled,
    required this.watchActive,
    required this.automaticSyncAvailable,
    required this.webhookUrlConfigured,
    required this.calendarId,
    required this.calendarName,
    required this.calendarTimeZone,
    required this.connectedAt,
    required this.lastSyncedAt,
    required this.lastSyncStatus,
    required this.watchExpiresAt,
    required this.pendingSync,
    required this.lastError,
  });

  factory GoogleCalendarConnectorStatus.fromJson(Map<String, dynamic> json) {
    return GoogleCalendarConnectorStatus(
      enabled: json['enabled'] as bool? ?? false,
      watchActive: json['watch_active'] as bool? ?? false,
      automaticSyncAvailable:
          json['automatic_sync_available'] as bool? ?? false,
      webhookUrlConfigured: json['webhook_url_configured'] as bool? ?? false,
      calendarId: json['calendar_id'] as String? ?? 'primary',
      calendarName: json['calendar_name'] as String? ?? 'Primary',
      calendarTimeZone: json['calendar_time_zone'] as String?,
      connectedAt: _parseDateTime(json['connected_at'] as String?),
      lastSyncedAt: _parseDateTime(json['last_synced_at'] as String?),
      lastSyncStatus: json['last_sync_status'] as String?,
      watchExpiresAt: _parseDateTime(json['watch_expires_at'] as String?),
      pendingSync: json['pending_sync'] as bool? ?? false,
      lastError: json['last_error'] as String?,
    );
  }

  static DateTime? _parseDateTime(String? value) {
    if (value == null || value.isEmpty) return null;
    return DateTime.tryParse(value);
  }
}

class GmailConnectorStatus {
  final bool enabled;
  final String? emailAddress;
  final DateTime? connectedAt;
  final String? lastError;

  const GmailConnectorStatus({
    required this.enabled,
    required this.emailAddress,
    required this.connectedAt,
    required this.lastError,
  });

  factory GmailConnectorStatus.fromJson(Map<String, dynamic> json) {
    return GmailConnectorStatus(
      enabled: json['enabled'] as bool? ?? false,
      emailAddress: json['email_address'] as String?,
      connectedAt: _parseDateTime(json['connected_at'] as String?),
      lastError: json['last_error'] as String?,
    );
  }

  static DateTime? _parseDateTime(String? value) {
    if (value == null || value.isEmpty) return null;
    return DateTime.tryParse(value);
  }
}

class ConnectorsCatalog {
  final GoogleCalendarConnectorStatus googleCalendar;
  final GmailConnectorStatus gmail;

  const ConnectorsCatalog({
    required this.googleCalendar,
    required this.gmail,
  });

  factory ConnectorsCatalog.fromJson(Map<String, dynamic> json) {
    return ConnectorsCatalog(
      googleCalendar: GoogleCalendarConnectorStatus.fromJson(
        json['google_calendar'] as Map<String, dynamic>? ?? const {},
      ),
      gmail: GmailConnectorStatus.fromJson(
        json['gmail'] as Map<String, dynamic>? ?? const {},
      ),
    );
  }
}
