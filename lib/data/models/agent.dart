import 'package:flutter/material.dart';

enum AgentTapBehavior { chatThread, customScreen }

/// Static metadata for one agent tile on the Agents grid.
class Agent {
  final String id;
  final String name;
  final String subtitle;
  final IconData icon;
  final Color color;
  final AgentTapBehavior tapBehavior;

  const Agent({
    required this.id,
    required this.name,
    required this.subtitle,
    required this.icon,
    required this.color,
    this.tapBehavior = AgentTapBehavior.chatThread,
  });
}

/// All agents shown on the Agents grid, in display order.
/// Connectors keeps its existing custom screen.
/// The other agents open a per-agent chat thread.
///
/// Deep-link contract: each agent's [id] must match the `agent_id` string
/// the backend sends in FCM data payloads for notification routing to work.
const List<Agent> kAgents = [
  Agent(
    id: 'sports',
    name: 'MatchPoint',
    subtitle: 'Live scores & digests',
    icon: Icons.sports_rounded,
    color: Color(0xFF1B5E20),
  ),
  Agent(
    id: 'technews',
    name: 'BytePulse',
    subtitle: 'AI & tech news',
    icon: Icons.bolt_rounded,
    color: Color(0xFF0D47A1),
  ),
  Agent(
    id: 'posts',
    name: 'Tweeter',
    subtitle: 'Draft your tweets',
    icon: Icons.edit_note_rounded,
    color: Color(0xFF4A148C),
  ),
  Agent(
    id: 'connectors',
    name: 'Connectors',
    subtitle: 'Calendar, Gmail & more',
    icon: Icons.hub_rounded,
    color: Color(0xFF283593),
    tapBehavior: AgentTapBehavior.customScreen,
  ),
];
