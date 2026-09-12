import 'dart:async';

import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';
import 'package:intl/intl.dart';
import 'package:provider/provider.dart';
import 'package:url_launcher/url_launcher.dart';

import '../../../core/theme/app_colors.dart';
import '../../../data/models/connector_models.dart';
import '../../../data/services/deep_link_service.dart';
import '../../viewmodels/auth_viewmodel.dart';
import '../../viewmodels/connectors_viewmodel.dart';
import '../../widgets/error_display.dart';
import '../../widgets/loading_indicator.dart';
import '../../widgets/sign_in_required_view.dart';

class ConnectorsScreen extends StatefulWidget {
  const ConnectorsScreen({super.key});

  @override
  State<ConnectorsScreen> createState() => _ConnectorsScreenState();
}

class _ConnectorsScreenState extends State<ConnectorsScreen>
    with WidgetsBindingObserver {
  StreamSubscription<String>? _deepLinkSub;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    // The browser hop for Notion leaves the app entirely. Coming back is the
    // signal that something may have changed, whichever way the user returns:
    // the deep link, the back button, or the app switcher. The deep link below
    // only makes it immediate, it is not load-bearing on its own, because an
    // expired attempt renders a terminal page and emits no link at all.
    _deepLinkSub = DeepLinkService.instance.launchActions.listen((action) {
      if (action != DeepLinkService.launchActionConnectorsRefresh) return;
      _reload();
    });
    WidgetsBinding.instance.addPostFrameCallback((_) {
      // Skip the backend load for a logged-out guest — it would just 401 and
      // surface an error. The build gates the body to a sign-in prompt instead.
      if (!mounted) return;
      if (context.read<AuthViewModel>().user == null) return;
      context.read<ConnectorsViewModel>().load();
    });
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state == AppLifecycleState.resumed) _reload();
  }

  void _reload() {
    if (!mounted) return;
    if (context.read<AuthViewModel>().user == null) return;
    unawaited(context.read<ConnectorsViewModel>().load());
  }

  /// Notion is the one connector authorized in a browser rather than natively,
  /// so the screen owns the hop out. The ViewModel decides whether a hop is even
  /// needed: when tokens are still on file it just re-enables and returns null.
  Future<void> _connectNotion(ConnectorsViewModel vm) async {
    final url = await vm.connectNotion();
    if (!mounted || url == null) return;
    final uri = Uri.tryParse(url);
    if (uri == null) return;
    try {
      await launchUrl(uri, mode: LaunchMode.externalApplication);
    } catch (_) {
      // No browser, or the launch was refused. The card stays as it was and the
      // user can tap again; silently doing nothing would look like a dead button.
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(
          content: Text("Couldn't open Notion. Try again in a moment."),
        ),
      );
    }
  }

  @override
  void dispose() {
    _deepLinkSub?.cancel();
    WidgetsBinding.instance.removeObserver(this);
    super.dispose();
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
          // Guest (logged-out) user reached this via Settings. Offer sign-in
          // instead of a 401 error from the connector load.
          if (context.read<AuthViewModel>().user == null) {
            return SignInRequiredView(
              icon: Icons.hub_rounded,
              message:
                  'Sign in to connect your calendar, Gmail, and more to Buddy.',
              onSignIn: () => context.go('/login'),
            );
          }

          if (vm.state == ViewState.loading && !vm.googleCalendar.enabled) {
            return const FullScreenLoader(message: 'Loading connectors...');
          }

          return ListView(
            padding: EdgeInsets.fromLTRB(
              16,
              16,
              16,
              MediaQuery.viewPaddingOf(context).bottom + 16,
            ),
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
              _NotionCard(
                status: vm.notion,
                busy: vm.isMutating,
                onConnect: () => _connectNotion(vm),
                onDisconnect: vm.disconnectNotion,
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

/// Notion. The only connector here authorized in a browser rather than through
/// native sign-in, so it carries a button instead of a switch: a switch implies
/// the change happens on the spot, and this one leaves the app.
class _NotionCard extends StatelessWidget {
  final NotionConnectorStatus status;
  final bool busy;
  final Future<void> Function() onConnect;
  final Future<void> Function() onDisconnect;

  const _NotionCard({
    required this.status,
    required this.busy,
    required this.onConnect,
    required this.onDisconnect,
  });

  @override
  Widget build(BuildContext context) {
    final connectedLabel = _formatDateTime(status.connectedAt);
    final needsReauth = status.needsReauthorization;

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
                  child: Image.asset('assets/icons/notion.png'),
                ),
              ),
              const SizedBox(width: 12),
              const Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      'Notion',
                      style: TextStyle(
                        color: AppColors.textPrimary,
                        fontSize: 17,
                        fontWeight: FontWeight.w600,
                      ),
                    ),
                    SizedBox(height: 2),
                    // Says only what the phone actually does. Saving notes by
                    // voice is desktop-only (ECOSYSTEM.md contract 7d), so it is
                    // deliberately not promised here.
                    Text(
                      'Send research briefs straight into your workspace.',
                      style: TextStyle(
                        color: AppColors.textSecondary,
                        fontSize: 13,
                      ),
                    ),
                  ],
                ),
              ),
              _NotionAction(
                busy: busy,
                connected: status.enabled,
                needsReauth: needsReauth,
                onConnect: onConnect,
                onDisconnect: onDisconnect,
              ),
            ],
          ),
          const SizedBox(height: 16),
          _MetaRow(
            label: 'Workspace',
            value: status.workspaceName ?? 'Not connected',
          ),
          _MetaRow(
            label: 'Connected',
            value: connectedLabel ?? 'Not connected yet',
          ),
          if (status.lastError != null && status.lastError!.isNotEmpty)
            Padding(
              padding: const EdgeInsets.only(top: 10),
              child: Text(
                needsReauth
                    ? 'Notion needs you to authorize again.'
                    : status.lastError!,
                style: const TextStyle(
                  color: AppColors.warning,
                  fontSize: 12,
                ),
              ),
            ),
        ],
      ),
    );
  }

  static String? _formatDateTime(DateTime? value) {
    if (value == null) return null;
    return DateFormat('MMM d, h:mm a').format(value.toLocal());
  }
}

class _NotionAction extends StatelessWidget {
  final bool busy;
  final bool connected;
  final bool needsReauth;
  final Future<void> Function() onConnect;
  final Future<void> Function() onDisconnect;

  const _NotionAction({
    required this.busy,
    required this.connected,
    required this.needsReauth,
    required this.onConnect,
    required this.onDisconnect,
  });

  @override
  Widget build(BuildContext context) {
    if (busy) {
      return const SizedBox(
        width: 20,
        height: 20,
        child: CircularProgressIndicator(strokeWidth: 2),
      );
    }
    final label = connected && !needsReauth
        ? 'Disconnect'
        : needsReauth
            ? 'Reconnect'
            : 'Connect';
    final destructive = connected && !needsReauth;
    return TextButton(
      onPressed: () => destructive ? onDisconnect() : onConnect(),
      style: TextButton.styleFrom(
        foregroundColor: destructive ? AppColors.error : AppColors.accent,
        padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
      ),
      child: Text(
        label,
        style: const TextStyle(fontSize: 13.5, fontWeight: FontWeight.w600),
      ),
    );
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
