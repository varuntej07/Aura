import 'dart:async';

import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';
import 'package:provider/provider.dart';

import '../../../core/theme/app_colors.dart';
import '../../../data/models/reminder_model.dart';
import '../../../data/services/alarm_service.dart';
import '../../viewmodels/auth_viewmodel.dart';
import '../../viewmodels/reminders_viewmodel.dart';
import '../../widgets/sign_in_required_view.dart';

/// Full-page reminders list, accessible from Settings → Reminders.
///
/// Status semantics:
///   pending / snoozed / fired -> "Upcoming"  (user has not acknowledged yet)
///   dismissed -> "Completed" (user explicitly tapped to mark done)
///
/// Tapping an upcoming reminder marks it dismissed (with a 340 ms animation).
/// Tapping a completed reminder reverts it to active (instant undo).
/// New pages load automatically when the user scrolls within 200 px of the bottom.
class RemindersScreen extends StatelessWidget {
  const RemindersScreen({super.key});

  @override
  Widget build(BuildContext context) {
    return const _RemindersView();
  }
}

// View    

class _RemindersView extends StatefulWidget {
  const _RemindersView();

  @override
  State<_RemindersView> createState() => _RemindersViewState();
}

class _RemindersViewState extends State<_RemindersView> {
  final _scrollController = ScrollController();

  /// Whether the OS will let an alarm actually ring on this device. Re-read on
  /// every open, because the permission is granted in a system Settings screen
  /// and nothing notifies the app when it changes.
  bool _canRingAlarms = true;

  @override
  void initState() {
    super.initState();
    _scrollController.addListener(_onScroll);
    WidgetsBinding.instance.addPostFrameCallback((_) {
      final uid = context.read<AuthViewModel>().user?.uid;
      if (uid != null) {
        // Force a refetch on every open so reminders created elsewhere
        // (e.g. via Aura's save_reminder tool) show without an app restart.
        context.read<RemindersViewModel>().refreshReminders(uid);
      }
      unawaited(_refreshAlarmCapability());
    });
  }

  Future<void> _refreshAlarmCapability() async {
    if (!AlarmService.isSupported) return;
    final caps = await context.read<AlarmService>().refreshCapabilities();
    if (!mounted) return;
    setState(() => _canRingAlarms = caps.canRing);
  }

  @override
  void dispose() {
    _scrollController.dispose();
    super.dispose();
  }

  void _onScroll() {
    final pos = _scrollController.position;
    if (pos.pixels >= pos.maxScrollExtent - 200) {
      final uid = context.read<AuthViewModel>().user?.uid;
      if (uid != null) {
        // loadMore guards against concurrent calls internally.
        context.read<RemindersViewModel>().loadMore(uid);
      }
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: AppColors.deepBackground,
      appBar: AppBar(
        title: const Text('Reminders'),
        backgroundColor: AppColors.deepBackground,
        foregroundColor: AppColors.textPrimary,
        elevation: 0,
        leading: IconButton(
          icon: const Icon(Icons.arrow_back_ios_new, size: 18),
          onPressed: () => Navigator.pop(context),
        ),
      ),
      body: Consumer<RemindersViewModel>(
        builder: (context, vm, _) {
          // Guest (logged-out) user reached this via Settings. Offer sign-in
          // instead of a misleading "no reminders yet" dead-end.
          final uid = context.read<AuthViewModel>().user?.uid;
          if (uid == null) {
            return SignInRequiredView(
              icon: Icons.notifications_none_outlined,
              message: 'Sign in to see and manage your reminders.',
              onSignIn: () => context.go('/login'),
            );
          }

          if (vm.state == ViewState.loading) {
            return const Center(
              child: CircularProgressIndicator(
                color: AppColors.accent,
                strokeWidth: 2,
              ),
            );
          }

          final active = vm.activeReminders;
          final completed = vm.completedReminders;

          Future<void> onRefresh() => vm.refreshReminders(uid);

          if (active.isEmpty && completed.isEmpty) {
            // Wrapped in a scroll view so pull-to-refresh works on the empty
            // state too, the empty message still fills the viewport.
            return RefreshIndicator(
              onRefresh: onRefresh,
              color: AppColors.accent,
              backgroundColor: AppColors.deepBackground,
              child: LayoutBuilder(
                builder: (context, constraints) => ListView(
                  controller: _scrollController,
                  physics: const AlwaysScrollableScrollPhysics(),
                  children: [
                    ConstrainedBox(
                      constraints:
                          BoxConstraints(minHeight: constraints.maxHeight),
                      child: const _EmptyState(),
                    ),
                  ],
                ),
              ),
            );
          }

          return RefreshIndicator(
            onRefresh: onRefresh,
            color: AppColors.accent,
            backgroundColor: AppColors.deepBackground,
            child: ListView(
            controller: _scrollController,
            physics: const AlwaysScrollableScrollPhysics(),
            padding: const EdgeInsets.symmetric(vertical: 8),
            children: [
              if (vm.errorMessage != null)
                _ErrorBanner(
                  message: vm.errorMessage!,
                  onDismiss: vm.clearError,
                ),

              // Only when there is actually an alarm to be let down by. Nagging
              // about a permission on a screen full of silent reminders would be
              // noise; here it explains why a specific alarm below will not ring.
              if (!_canRingAlarms && active.any((r) => r.isAlarm))
                _AlarmPermissionBanner(
                  onGrant: () async {
                    await context.read<AlarmService>().requestExactAlarmAccess();
                    // The user returns from a system screen, so re-read rather
                    // than assume they granted it.
                    await _refreshAlarmCapability();
                  },
                ),

              // Active reminders 
              if (active.isNotEmpty) ...[
                const _SectionHeader('Upcoming'),
                ...active.map(
                  (r) => _ReminderTile(
                    key: ValueKey(r.id),
                    reminder: r,
                    isCompleted: false,
                    onComplete: () => vm.markComplete(uid, r.id),
                    onUndo: null,
                  ),
                ),
              ],

              // Completed reminders 
              if (completed.isNotEmpty) ...[
                const _SectionHeader('Completed'),
                ...completed.map(
                  (r) => _ReminderTile(
                    key: ValueKey(r.id),
                    reminder: r,
                    isCompleted: true,
                    onComplete: null,
                    onUndo: () => vm.markIncomplete(uid, r.id),
                  ),
                ),
              ],

              // Load-more indicator 
              if (vm.isLoadingMore)
                const Padding(
                  padding: EdgeInsets.symmetric(vertical: 20),
                  child: Center(
                    child: CircularProgressIndicator(
                      color: AppColors.accent,
                      strokeWidth: 2,
                    ),
                  ),
                ),

              SizedBox(height: MediaQuery.of(context).viewPadding.bottom + 96),
            ],
            ),
          );
        },
      ),
    );
  }
}

// Section header 

class _SectionHeader extends StatelessWidget {
  final String title;

  const _SectionHeader(this.title);

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.fromLTRB(16, 20, 16, 8),
      child: Text(
        title.toUpperCase(),
        style: const TextStyle(
          color: AppColors.textTertiary,
          fontSize: 11,
          fontWeight: FontWeight.w600,
          letterSpacing: 1.2,
        ),
      ),
    );
  }
}

// Reminder tile 

class _ReminderTile extends StatefulWidget {
  final ReminderModel reminder;
  final bool isCompleted;

  /// Called after the completion animation (340 ms delay) on upcoming tiles.
  final VoidCallback? onComplete;

  /// Called immediately on completed tiles — instant undo, no animation delay.
  final VoidCallback? onUndo;

  const _ReminderTile({
    super.key,
    required this.reminder,
    required this.isCompleted,
    required this.onComplete,
    required this.onUndo,
  });

  @override
  State<_ReminderTile> createState() => _ReminderTileState();
}

class _ReminderTileState extends State<_ReminderTile> {
  /// True between the user's tap and the ViewModel rebuilding — lets the
  /// checked / strikethrough animation play before the list restructures.
  bool _completing = false;

  bool get _showAsCompleted => widget.isCompleted || _completing;

  // Tap handlers 

  Future<void> _handleComplete() async {
    if (_completing || widget.onComplete == null) return;
    setState(() => _completing = true);
    await Future<void>.delayed(const Duration(milliseconds: 340));
    widget.onComplete?.call();
  }

  void _handleUndo() => widget.onUndo?.call();

  // Formatting 

  String _formatTime(DateTime dt) {
    final local = dt.toLocal();
    final now = DateTime.now();
    final isToday = local.year == now.year &&
        local.month == now.month &&
        local.day == now.day;
    final yesterday = now.subtract(const Duration(days: 1));
    final isYesterday = local.year == yesterday.year &&
        local.month == yesterday.month &&
        local.day == yesterday.day;

    final hour = local.hour % 12 == 0 ? 12 : local.hour % 12;
    final minute = local.minute.toString().padLeft(2, '0');
    final ampm = local.hour < 12 ? 'AM' : 'PM';
    final timeStr = '$hour:$minute $ampm';

    if (isToday) return 'Today · $timeStr';
    if (isYesterday) return 'Yesterday · $timeStr';
    const months = [
      'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
      'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec',
    ];
    return '${months[local.month - 1]} ${local.day} · $timeStr';
  }

  @override
  Widget build(BuildContext context) {
    // Show "Notified" badge only on fired reminders that haven't been
    // completed or started completing yet.
    final showNotifiedBadge =
        widget.reminder.status == ReminderStatus.fired &&
            !widget.isCompleted &&
            !_completing;

    return GestureDetector(
      onTap: widget.isCompleted ? _handleUndo : _handleComplete,
      child: AnimatedContainer(
        duration: const Duration(milliseconds: 220),
        curve: Curves.easeOut,
        margin: const EdgeInsets.symmetric(horizontal: 16, vertical: 3),
        padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 14),
        decoration: BoxDecoration(
          gradient: LinearGradient(
            begin: Alignment.topLeft,
            end: Alignment.bottomRight,
            colors: [
              AppColors.textPrimary.withValues(alpha: _showAsCompleted ? 0.03 : 0.07),
              AppColors.textPrimary.withValues(alpha: _showAsCompleted ? 0.01 : 0.03),
            ],
          ),
          borderRadius: BorderRadius.circular(12),
          border: Border.all(
            color: AppColors.textPrimary.withValues(alpha: _showAsCompleted ? 0.05 : 0.12),
          ),
        ),
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Padding(
              padding: const EdgeInsets.only(top: 1),
              child: _CompletionCircle(
                isCompleted: _showAsCompleted,
                color: AppColors.accent,
              ),
            ),
            const SizedBox(width: 12),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    widget.reminder.message,
                    style: TextStyle(
                      color: _showAsCompleted
                          ? AppColors.textTertiary
                          : AppColors.textPrimary,
                      fontSize: 14,
                      fontWeight: FontWeight.w500,
                      decoration: _showAsCompleted
                          ? TextDecoration.lineThrough
                          : TextDecoration.none,
                      decorationColor: AppColors.textTertiary,
                      height: 1.4,
                    ),
                  ),
                  const SizedBox(height: 4),
                  Row(
                    children: [
                      Text(
                        _formatTime(widget.reminder.triggerAt),
                        style: const TextStyle(
                          color: AppColors.textTertiary,
                          fontSize: 12,
                        ),
                      ),
                      if (showNotifiedBadge) ...[
                        const SizedBox(width: 8),
                        const _NotifiedBadge(),
                      ],
                      // Subtle hint on completed tiles so the user discovers undo.
                      if (widget.isCompleted) ...[
                        const Spacer(),
                        const Text(
                          'Tap to undo',
                          style: TextStyle(
                            color: AppColors.textTertiary,
                            fontSize: 11,
                          ),
                        ),
                      ],
                    ],
                  ),
                ],
              ),
            ),
          ],
        ),
      ),
    );
  }
}

// Completion circle 

class _CompletionCircle extends StatelessWidget {
  final bool isCompleted;
  final Color color;

  const _CompletionCircle({required this.isCompleted, required this.color});

  @override
  Widget build(BuildContext context) {
    return AnimatedContainer(
      duration: const Duration(milliseconds: 200),
      width: 20,
      height: 20,
      decoration: BoxDecoration(
        shape: BoxShape.circle,
        color: isCompleted ? color.withValues(alpha: 0.2) : Colors.transparent,
        border: Border.all(
          color: isCompleted ? color.withValues(alpha: 0.4) : color,
          width: 1.8,
        ),
      ),
      child: isCompleted
          ? Icon(Icons.check, size: 12, color: color)
          : null,
    );
  }
}

// Notified badge 

/// Shown on fired reminders in the Upcoming section — the notification
/// was delivered but the user has not tapped to acknowledge yet.
/// Uses warm amber to signal "something happened, needs attention"
/// without being alarming.
class _NotifiedBadge extends StatelessWidget {
  const _NotifiedBadge();

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 2),
      decoration: BoxDecoration(
        color: AppColors.warning.withValues(alpha: 0.12),
        borderRadius: BorderRadius.circular(4),
      ),
      child: const Text(
        'Notified',
        style: TextStyle(
          color: AppColors.warning,
          fontSize: 10,
          fontWeight: FontWeight.w600,
          letterSpacing: 0.2,
        ),
      ),
    );
  }
}

// Empty state 

class _EmptyState extends StatelessWidget {
  const _EmptyState();

  @override
  Widget build(BuildContext context) {
    return Center(
      child: Column(
        mainAxisAlignment: MainAxisAlignment.center,
        children: [
          const Icon(
            Icons.notifications_none_outlined,
            size: 56,
            color: AppColors.textTertiary,
          ),
          const SizedBox(height: 16),
          const Text(
            'No reminders yet',
            style: TextStyle(
              color: AppColors.textSecondary,
              fontSize: 16,
              fontWeight: FontWeight.w500,
            ),
          ),
          const SizedBox(height: 8),
          const Text(
            'Ask Aura to remind you of something.',
            style: TextStyle(
              color: AppColors.textTertiary,
              fontSize: 13,
            ),
          ),
        ],
      ),
    );
  }
}

// Alarm permission banner

/// Shown when an alarm exists but the OS has not granted exact-alarm access.
///
/// The alarm below it is not silent, but it is armed inexactly and can fire
/// several minutes late, so this says what is actually true rather than hiding a
/// degraded promise behind an alarm icon.
class _AlarmPermissionBanner extends StatelessWidget {
  final Future<void> Function() onGrant;

  const _AlarmPermissionBanner({required this.onGrant});

  @override
  Widget build(BuildContext context) {
    return Container(
      margin: const EdgeInsets.fromLTRB(16, 8, 16, 8),
      padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 12),
      decoration: BoxDecoration(
        color: AppColors.errorSurface,
        borderRadius: BorderRadius.circular(10),
        border: Border.all(color: AppColors.warning.withValues(alpha: 0.35)),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              const Icon(
                Icons.alarm_off_outlined,
                size: 16,
                color: AppColors.warning,
              ),
              const SizedBox(width: 8),
              Expanded(
                child: Text(
                  'Your alarms may fire a few minutes late',
                  style: const TextStyle(
                    color: AppColors.textPrimary,
                    fontSize: 13,
                    fontWeight: FontWeight.w600,
                  ),
                ),
              ),
            ],
          ),
          const SizedBox(height: 6),
          const Text(
            'Android needs permission before Buddy can ring exactly on time.',
            style: TextStyle(color: AppColors.textSecondary, fontSize: 12.5),
          ),
          const SizedBox(height: 10),
          Align(
            alignment: Alignment.centerLeft,
            child: TextButton(
              onPressed: () => unawaited(onGrant()),
              style: TextButton.styleFrom(
                padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
                minimumSize: Size.zero,
                tapTargetSize: MaterialTapTargetSize.shrinkWrap,
              ),
              child: const Text(
                'Turn on alarms',
                style: TextStyle(
                  color: AppColors.accent,
                  fontSize: 13,
                  fontWeight: FontWeight.w600,
                ),
              ),
            ),
          ),
        ],
      ),
    );
  }
}

// Error banner

class _ErrorBanner extends StatelessWidget {
  final String message;
  final VoidCallback onDismiss;

  const _ErrorBanner({required this.message, required this.onDismiss});

  @override
  Widget build(BuildContext context) {
    return Container(
      margin: const EdgeInsets.fromLTRB(16, 8, 16, 8),
      padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 10),
      decoration: BoxDecoration(
        color: AppColors.errorSurface,
        borderRadius: BorderRadius.circular(10),
        border: Border.all(color: AppColors.error.withValues(alpha: 0.3)),
      ),
      child: Row(
        children: [
          const Icon(
            Icons.warning_amber_outlined,
            size: 16,
            color: AppColors.warning,
          ),
          const SizedBox(width: 8),
          Expanded(
            child: Text(
              message,
              style: const TextStyle(
                color: AppColors.textSecondary,
                fontSize: 13,
              ),
            ),
          ),
          GestureDetector(
            onTap: onDismiss,
            child: const Icon(
              Icons.close,
              size: 16,
              color: AppColors.textTertiary,
            ),
          ),
        ],
      ),
    );
  }
}
