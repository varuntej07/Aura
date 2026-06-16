import 'package:flutter/foundation.dart';

/// Which proactive notification opened the chat.
///
/// Every value maps to exactly one seeded-opener method on `ChatViewModel`, and
/// `ChatScreen` switches on this enum exhaustively. Adding a new proactive
/// decider therefore surfaces as a COMPILE-time non-exhaustive-switch warning if
/// its branch is forgotten, instead of a silently empty chat — the exact failure
/// the icebreaker hit when it routed to `/chat/new` with no handler downstream.
enum NotificationChatOrigin { engagement, signal, thread, icebreaker, briefing, tracker }

/// The single typed object carried in `/chat/new` route extras when a proactive
/// notification is tapped. It replaces the four ad-hoc `extra` maps
/// (`engagementId` / `signalNotificationId` / `threadFollowUpId` /
/// `icebreakerNotificationId`) so all deciders open chat through one choke point.
///
/// Fields are origin-specific; an origin only reads the ones it needs and leaves
/// the rest at their empty defaults. `openingMessage` is the one field every
/// origin uses — the message Buddy opens the chat with (the notification's whole
/// reason to exist).
@immutable
class NotificationChatSeed {
  final NotificationChatOrigin origin;

  /// The opener Buddy shows as the first bubble in the seeded chat.
  final String openingMessage;

  /// An optional user message to auto-send as the first turn once the chat opens.
  /// Used by the daily-briefing FAB handoff: the user types in the in-place input,
  /// and that text is sent as their opening reply so the conversation continues
  /// seamlessly into the full chat. Empty means "seed Buddy's opener only".
  final String firstUserMessage;

  /// Funnel/join key for the re-engagement funnel (signal, icebreaker).
  final String notificationId;

  /// Signal-engine content attribution.
  final String contentId;
  final String category;

  /// Engagement-chain attribution + agent context for the opener bubble.
  final String engagementId;
  final String agentContext;

  /// Curiosity-thread id; the chat reconciles any server-side shade exchange and
  /// renders [suggestedReplies] as pills.
  final String threadId;
  final List<String> suggestedReplies;

  const NotificationChatSeed({
    required this.origin,
    this.openingMessage = '',
    this.firstUserMessage = '',
    this.notificationId = '',
    this.contentId = '',
    this.category = '',
    this.engagementId = '',
    this.agentContext = '',
    this.threadId = '',
    this.suggestedReplies = const [],
  });
}
