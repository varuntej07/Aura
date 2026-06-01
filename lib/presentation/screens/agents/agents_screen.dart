import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';
import 'package:provider/provider.dart';

import '../../../core/theme/app_colors.dart';
import '../../../core/theme/glass_card.dart';
import '../../../data/models/agent.dart';
import '../../viewmodels/connectors_viewmodel.dart';
import '../connectors/connectors_screen.dart';

class AgentsScreen extends StatefulWidget {
  const AgentsScreen({super.key});

  @override
  State<AgentsScreen> createState() => _AgentsScreenState();
}

class _AgentsScreenState extends State<AgentsScreen> {
  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addPostFrameCallback((_) {
      context.read<ConnectorsViewModel>().load();
    });
  }

  void _handleAgentTap(BuildContext context, Agent agent) {
    if (agent.tapBehavior == AgentTapBehavior.chatThread) {
      context.push('/agents/${agent.id}');
      return;
    }

    // Custom screen for Connectors
    switch (agent.id) {
      case 'connectors':
        Navigator.of(context, rootNavigator: true).push(
          MaterialPageRoute<void>(builder: (_) => const ConnectorsScreen()),
        );
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: Colors.transparent,
      body: SafeArea(
        bottom: false,
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Padding(
              padding: const EdgeInsets.fromLTRB(24, 20, 24, 4),
              child: const Text(
                'Agents',
                style: TextStyle(
                  color: AppColors.textPrimary,
                  fontSize: 28,
                  fontWeight: FontWeight.w700,
                  letterSpacing: -0.8,
                ),
              ),
            ),
            Padding(
              padding: const EdgeInsets.fromLTRB(24, 0, 24, 20),
              child: const Text(
                'Specialized AI for every domain',
                style: TextStyle(
                  color: AppColors.textTertiary,
                  fontSize: 14,
                ),
              ),
            ),
            Expanded(
              child: GridView.builder(
                padding: const EdgeInsets.fromLTRB(20, 0, 20, 100),
                gridDelegate:
                    const SliverGridDelegateWithFixedCrossAxisCount(
                  crossAxisCount: 3,
                  mainAxisSpacing: 20,
                  crossAxisSpacing: 12,
                  childAspectRatio: 0.82,
                ),
                itemCount: kAgents.length,
                itemBuilder: (context, i) {
                  final agent = kAgents[i];
                  return _AgentTile(
                    agent: agent,
                    onTap: () => _handleAgentTap(context, agent),
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

// Agent tile

class _AgentTile extends StatelessWidget {
  final Agent agent;
  final VoidCallback onTap;

  const _AgentTile({required this.agent, required this.onTap});

  @override
  Widget build(BuildContext context) {
    return GestureDetector(
      onTap: onTap,
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          Hero(
            tag: 'agent-icon-${agent.id}',
            child: FauxGlassCard(
              borderRadius: 22,
              padding: const EdgeInsets.all(0),
              borderColor: agent.color.withValues(alpha: 0.35),
              gradient: LinearGradient(
                begin: Alignment.topLeft,
                end: Alignment.bottomRight,
                colors: [
                  agent.color.withValues(alpha: 0.25),
                  agent.color.withValues(alpha: 0.12),
                ],
              ),
              child: SizedBox(
                width: 76,
                height: 76,
                child: Center(
                  child: Icon(agent.icon, color: Colors.white, size: 32),
                ),
              ),
            ),
          ),
          const SizedBox(height: 8),
          Text(
            agent.name,
            style: const TextStyle(
              color: AppColors.textPrimary,
              fontSize: 12,
              fontWeight: FontWeight.w500,
            ),
            maxLines: 1,
            overflow: TextOverflow.ellipsis,
            textAlign: TextAlign.center,
          ),
        ],
      ),
    );
  }
}
