import 'package:flutter/material.dart';
import 'package:intl/intl.dart';
import 'package:provider/provider.dart';

import '../../../core/theme/app_colors.dart';
import '../../../data/models/connector_models.dart';
import '../../viewmodels/connectors_viewmodel.dart';
import '../../widgets/error_display.dart';
import '../../widgets/loading_indicator.dart';

class ConnectorsScreen extends StatefulWidget {
  const ConnectorsScreen({super.key});

  @override
  State<ConnectorsScreen> createState() => _ConnectorsScreenState();
}

class _ConnectorsScreenState extends State<ConnectorsScreen> {
  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addPostFrameCallback((_) {
      context.read<ConnectorsViewModel>().load();
    });
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: AppColors.background,
      appBar: AppBar(
        title: const Text('Connectors'),
        backgroundColor: AppColors.background,
        foregroundColor: AppColors.textPrimary,
        elevation: 0,
      ),
      body: Consumer<ConnectorsViewModel>(
        builder: (context, vm, _) {
          if (vm.state == ViewState.loading && !vm.googleCalendar.enabled) {
            return const FullScreenLoader(message: 'Loading connectors...');
          }

          return ListView(
            padding: const EdgeInsets.all(16),
            children: [
              if (vm.error != null)
                Padding(
                  padding: const EdgeInsets.only(bottom: 16),
                  child: ErrorDisplay(
                    error: vm.error!,
                    onDismiss: vm.clearError,
                  ),
                ),
              _GoogleCalendarCard(
                status: vm.googleCalendar,
                busy: vm.isMutating,
                onToggle: vm.toggleGoogleCalendar,
                onSync: vm.syncGoogleCalendar,
              ),
              const SizedBox(height: 16),
              _GmailCard(
                status: vm.gmail,
                busy: vm.isMutating,
                onToggle: vm.toggleGmail,
                comingSoon: true,
              ),
              const SizedBox(height: 16),
              const _ComingSoonConnectorCard(
                iconAsset: 'assets/icons/todoist.png',
                title: 'Todoist',
                subtitle: 'Let Buddy add and check off your tasks.',
              ),
              const SizedBox(height: 16),
              const _ComingSoonConnectorCard(
                iconAsset: 'assets/icons/notion.png',
                title: 'Notion',
                subtitle: 'Capture notes and pull in your pages.',
              ),
              const SizedBox(height: 16),
              const _ComingSoonConnectorCard(
                iconAsset: 'assets/icons/spotify.png',
                title: 'Spotify',
                subtitle: 'Start focus playlists hands-free.',
              ),
              const SizedBox(height: 16),
              const _ComingSoonConnectorCard(
                iconAsset: 'assets/icons/slack.png',
                title: 'Slack',
                subtitle: 'Get nudges where you already work.',
              ),
              const SizedBox(height: 16),
              const _ComingSoonConnectorCard(
                iconAsset: 'assets/icons/oura.png',
                title: 'Oura',
                subtitle: 'Bring sleep and readiness into your day.',
              ),
            ],
          );
        },
      ),
    );
  }
}

class _GoogleCalendarCard extends StatelessWidget {
  final GoogleCalendarConnectorStatus status;
  final bool busy;
  final Future<void> Function(bool enabled) onToggle;
  final Future<void> Function() onSync;

  const _GoogleCalendarCard({
    required this.status,
    required this.busy,
    required this.onToggle,
    required this.onSync,
  });

  @override
  Widget build(BuildContext context) {
    final syncLabel = _formatDateTime(status.lastSyncedAt);
    final watchLabel = _formatDateTime(status.watchExpiresAt);

    return Container(
      padding: const EdgeInsets.all(18),
      decoration: BoxDecoration(
        color: AppColors.surface,
        borderRadius: BorderRadius.circular(20),
        border: Border.all(color: AppColors.border),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Container(
                width: 42,
                height: 42,
                decoration: BoxDecoration(
                  color: Colors.white,
                  borderRadius: BorderRadius.circular(12),
                ),
                child: Padding(
                  padding: const EdgeInsets.all(5),
                  child: Image.asset('assets/icons/google_calendar.png'),
                ),
              ),
              const SizedBox(width: 12),
              const Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      'Google Calendar',
                      style: TextStyle(
                        color: AppColors.textPrimary,
                        fontSize: 17,
                        fontWeight: FontWeight.w600,
                      ),
                    ),
                    SizedBox(height: 2),
                    Text(
                      'Sync meetings into Aura for chat answers.',
                      style: TextStyle(
                        color: AppColors.textSecondary,
                        fontSize: 13,
                      ),
                    ),
                  ],
                ),
              ),
              Switch(
                value: status.enabled,
                onChanged: busy ? null : onToggle,
                activeThumbColor: AppColors.accent,
              ),
            ],
          ),
          const SizedBox(height: 16),
          _MetaRow(
            label: 'Calendar',
            value: status.calendarName,
          ),
          _MetaRow(
            label: 'Last Sync',
            value: syncLabel ?? 'Not synced yet',
          ),
          _MetaRow(
            label: 'Auto Sync',
            value: status.watchActive
                ? 'Webhook active'
                : status.enabled
                ? 'Connected, waiting for public HTTPS webhook'
                : 'Disconnected',
          ),
          if (status.calendarTimeZone != null)
            _MetaRow(
              label: 'Timezone',
              value: status.calendarTimeZone!,
            ),
          if (watchLabel != null)
            _MetaRow(
              label: 'Watch Expires',
              value: watchLabel,
            ),
          if (status.pendingSync)
            const Padding(
              padding: EdgeInsets.only(top: 10),
              child: Text(
                'A calendar update is queued and will be processed shortly.',
                style: TextStyle(
                  color: AppColors.warning,
                  fontSize: 12,
                ),
              ),
            ),
          if (status.lastError != null && status.lastError!.isNotEmpty)
            Padding(
              padding: const EdgeInsets.only(top: 10),
              child: Text(
                status.lastError!,
                style: const TextStyle(
                  color: AppColors.warning,
                  fontSize: 12,
                ),
              ),
            ),
          if (status.enabled) ...[
            const SizedBox(height: 16),
            SizedBox(
              width: double.infinity,
              child: OutlinedButton(
                onPressed: busy ? null : onSync,
                style: OutlinedButton.styleFrom(
                  foregroundColor: AppColors.textPrimary,
                  side: const BorderSide(color: AppColors.border),
                  padding: const EdgeInsets.symmetric(vertical: 14),
                ),
                child: busy
                    ? const SizedBox(
                        width: 18,
                        height: 18,
                        child: CircularProgressIndicator(strokeWidth: 2),
                      )
                    : const Text('Sync Now'),
              ),
            ),
          ],
        ],
      ),
    );
  }

  static String? _formatDateTime(DateTime? value) {
    if (value == null) return null;
    return DateFormat('MMM d, h:mm a').format(value.toLocal());
  }
}

class _GmailCard extends StatelessWidget {
  final GmailConnectorStatus status;
  final bool busy;
  final Future<void> Function(bool enabled) onToggle;
  // While true, the connector is shown as "Coming soon" and the toggle is
  // hidden. Gmail uses restricted OAuth scopes that need Google verification
  // (a CASA security assessment) before non-test users can connect — flip this
  // back to false once that's done to restore the live toggle.
  final bool comingSoon;

  const _GmailCard({
    required this.status,
    required this.busy,
    required this.onToggle,
    this.comingSoon = false,
  });

  @override
  Widget build(BuildContext context) {
    final connectedLabel = _formatDateTime(status.connectedAt);

    return Container(
      padding: const EdgeInsets.all(18),
      decoration: BoxDecoration(
        color: AppColors.surface,
        borderRadius: BorderRadius.circular(20),
        border: Border.all(color: AppColors.border),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Container(
                width: 42,
                height: 42,
                decoration: BoxDecoration(
                  color: Colors.white,
                  borderRadius: BorderRadius.circular(12),
                ),
                child: Padding(
                  padding: const EdgeInsets.all(5),
                  child: Image.asset('assets/icons/gmail.png'),
                ),
              ),
              const SizedBox(width: 12),
              const Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      'Gmail',
                      style: TextStyle(
                        color: AppColors.textPrimary,
                        fontSize: 17,
                        fontWeight: FontWeight.w600,
                      ),
                    ),
                    SizedBox(height: 2),
                    Text(
                      'Read and send email through Buddy.',
                      style: TextStyle(
                        color: AppColors.textSecondary,
                        fontSize: 13,
                      ),
                    ),
                  ],
                ),
              ),
              if (comingSoon)
                const _ComingSoonBadge()
              else
                Switch(
                  value: status.enabled,
                  onChanged: busy ? null : onToggle,
                  activeThumbColor: AppColors.accent,
                ),
            ],
          ),
          if (!comingSoon) ...[
            const SizedBox(height: 16),
            _MetaRow(
              label: 'Account',
              value: status.emailAddress ?? 'Not connected',
            ),
            _MetaRow(
              label: 'Connected',
              value: connectedLabel ?? 'Not connected yet',
            ),
            if (status.lastError != null && status.lastError!.isNotEmpty)
              Padding(
                padding: const EdgeInsets.only(top: 10),
                child: Text(
                  status.lastError!,
                  style: const TextStyle(
                    color: AppColors.warning,
                    fontSize: 12,
                  ),
                ),
              ),
          ],
        ],
      ),
    );
  }

  static String? _formatDateTime(DateTime? value) {
    if (value == null) return null;
    return DateFormat('MMM d, h:mm a').format(value.toLocal());
  }
}

/// Small pill shown in place of a connector's toggle while the integration
/// isn't available yet. Identical across every coming-soon connector.
class _ComingSoonBadge extends StatelessWidget {
  const _ComingSoonBadge();

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 5),
      decoration: BoxDecoration(
        color: AppColors.accent.withValues(alpha: 0.12),
        borderRadius: BorderRadius.circular(20),
        border: Border.all(color: AppColors.accent.withValues(alpha: 0.3)),
      ),
      child: const Text(
        'Coming soon',
        style: TextStyle(
          color: AppColors.accent,
          fontSize: 12,
          fontWeight: FontWeight.w600,
        ),
      ),
    );
  }
}

/// A connector tile for integrations that aren't live yet. Non-interactive —
/// it advertises the integration and carries the shared "Coming soon" badge.
class _ComingSoonConnectorCard extends StatelessWidget {
  final String iconAsset;
  final String title;
  final String subtitle;

  const _ComingSoonConnectorCard({
    required this.iconAsset,
    required this.title,
    required this.subtitle,
  });

  @override
  Widget build(BuildContext context) {
    return Opacity(
      opacity: 0.75,
      child: Container(
        padding: const EdgeInsets.all(18),
        decoration: BoxDecoration(
          color: AppColors.surface,
          borderRadius: BorderRadius.circular(20),
          border: Border.all(color: AppColors.border),
        ),
        child: Row(
          children: [
            Container(
              width: 42,
              height: 42,
              decoration: BoxDecoration(
                color: Colors.white,
                borderRadius: BorderRadius.circular(12),
              ),
              child: Padding(
                padding: const EdgeInsets.all(7),
                child: Image.asset(iconAsset),
              ),
            ),
            const SizedBox(width: 12),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    title,
                    style: const TextStyle(
                      color: AppColors.textPrimary,
                      fontSize: 17,
                      fontWeight: FontWeight.w600,
                    ),
                  ),
                  const SizedBox(height: 2),
                  Text(
                    subtitle,
                    style: const TextStyle(
                      color: AppColors.textSecondary,
                      fontSize: 13,
                    ),
                  ),
                ],
              ),
            ),
            const SizedBox(width: 12),
            const _ComingSoonBadge(),
          ],
        ),
      ),
    );
  }
}

class _MetaRow extends StatelessWidget {
  final String label;
  final String value;

  const _MetaRow({
    required this.label,
    required this.value,
  });

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.only(top: 8),
      child: Row(
        children: [
          SizedBox(
            width: 96,
            child: Text(
              label,
              style: const TextStyle(
                color: AppColors.textTertiary,
                fontSize: 12,
              ),
            ),
          ),
          Expanded(
            child: Text(
              value,
              style: const TextStyle(
                color: AppColors.textPrimary,
                fontSize: 13,
              ),
            ),
          ),
        ],
      ),
    );
  }
}
