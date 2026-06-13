import 'package:flutter/material.dart';

import '../../core/theme/app_colors.dart';
import '../../data/local/app_database.dart';

/// Slide-in history drawer used by both ChatScreen and AgentThreadScreen.
/// Receives data and callbacks from the parent — no direct ViewModel
/// dependency — so it works with any ChatViewModel subtype.
class ChatHistoryDrawer extends StatelessWidget {
  final List<ChatSession> sessions;
  final String? currentSessionId;
  final void Function(String sessionId) onSessionSelected;
  final VoidCallback onNewChat;

  const ChatHistoryDrawer({
    super.key,
    required this.sessions,
    required this.currentSessionId,
    required this.onSessionSelected,
    required this.onNewChat,
  });

  @override
  Widget build(BuildContext context) {
    return Drawer(
      backgroundColor: AppColors.surface,
      child: SafeArea(
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Padding(
              padding: const EdgeInsets.fromLTRB(20, 20, 20, 4),
              child: Row(
                mainAxisAlignment: MainAxisAlignment.spaceBetween,
                children: [
                  const Text(
                    'History',
                    style: TextStyle(
                      color: AppColors.textPrimary,
                      fontSize: 18,
                      fontWeight: FontWeight.w600,
                    ),
                  ),
                  TextButton(
                    onPressed: () {
                      Navigator.of(context).pop();
                      onNewChat();
                    },
                    style: TextButton.styleFrom(
                      foregroundColor: AppColors.accent,
                      padding: const EdgeInsets.symmetric(
                          horizontal: 12, vertical: 6),
                      minimumSize: Size.zero,
                      tapTargetSize: MaterialTapTargetSize.shrinkWrap,
                    ),
                    child: const Text(
                      'New Chat',
                      style: TextStyle(fontSize: 13, fontWeight: FontWeight.w500),
                    ),
                  ),
                ],
              ),
            ),
            const Divider(height: 1, color: AppColors.divider),
            Expanded(
              child: sessions.isEmpty
                  ? const Center(
                      child: Text(
                        'No previous conversations',
                        style: TextStyle(
                          color: AppColors.textTertiary,
                          fontSize: 13,
                        ),
                      ),
                    )
                  : ListView.builder(
                      padding: const EdgeInsets.symmetric(vertical: 8),
                      itemCount: sessions.length,
                      itemBuilder: (context, index) {
                        final session = sessions[index];
                        final isActive = session.id == currentSessionId;
                        return _SessionTile(
                          session: session,
                          isActive: isActive,
                          onTap: () {
                            Navigator.of(context).pop();
                            onSessionSelected(session.id);
                          },
                        );
                      },
                    ),
            ),
          ],
        ),
      ),
    );
  }
}

class _SessionTile extends StatelessWidget {
  final ChatSession session;
  final bool isActive;
  final VoidCallback onTap;

  const _SessionTile({
    required this.session,
    required this.isActive,
    required this.onTap,
  });

  @override
  Widget build(BuildContext context) {
    final title = (session.title?.trim().isNotEmpty == true)
        ? session.title!
        : 'New Chat';

    return InkWell(
      onTap: onTap,
      borderRadius: BorderRadius.circular(8),
      child: Container(
        margin: const EdgeInsets.symmetric(horizontal: 8, vertical: 2),
        padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
        decoration: BoxDecoration(
          color: isActive ? AppColors.accentGlow : Colors.transparent,
          borderRadius: BorderRadius.circular(8),
        ),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
              title,
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
              style: TextStyle(
                color: isActive ? AppColors.accentDark : AppColors.textPrimary,
                fontSize: 14,
                fontWeight:
                    isActive ? FontWeight.w500 : FontWeight.w400,
              ),
            ),
            const SizedBox(height: 2),
            Text(
              _formatDate(session.updatedAt),
              style: const TextStyle(
                color: AppColors.textTertiary,
                fontSize: 11,
              ),
            ),
          ],
        ),
      ),
    );
  }

  String _formatDate(DateTime date) {
    final now = DateTime.now();
    final today = DateTime(now.year, now.month, now.day);
    final sessionDay = DateTime(date.year, date.month, date.day);
    final diff = today.difference(sessionDay).inDays;

    if (diff == 0) return 'Today';
    if (diff == 1) return 'Yesterday';

    const months = [
      'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
      'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec',
    ];
    final label = '${months[date.month - 1]} ${date.day}';
    return date.year != now.year ? '$label, ${date.year}' : label;
  }
}
