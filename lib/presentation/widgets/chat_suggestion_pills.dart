import 'package:flutter/material.dart';

import '../../core/theme/app_colors.dart';
import '../../core/theme/glass_card.dart';

/// Centered empty-state starters for the Buddy chat. Renders up to 3 thin,
/// full-width tappable cards in the middle of the screen. 
/// Purely presentational: the personalized labels and the tap callback arrive via the constructor.
/// Tapping a card fills the message input (handled by the parent)
class ChatSuggestionPills extends StatelessWidget {
  const ChatSuggestionPills({
    super.key,
    required this.pills,
    required this.onTap,
  });

  final List<String> pills;
  final ValueChanged<String> onTap;

  @override
  Widget build(BuildContext context) {
    if (pills.isEmpty) return const SizedBox.shrink();
    final visible = pills.take(3).toList();

    return Center(
      child: SingleChildScrollView(
        padding: const EdgeInsets.symmetric(horizontal: 24, vertical: 24),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            for (final pill in visible)
              Padding(
                padding: const EdgeInsets.only(bottom: 10),
                child: GestureDetector(
                  onTap: () => onTap(pill),
                  child: FauxGlassCard(
                    borderRadius: 14,
                    padding: const EdgeInsets.symmetric(
                        horizontal: 16, vertical: 14),
                    child: Row(
                      children: [
                        Expanded(
                          child: Text(
                            pill,
                            style: const TextStyle(
                              color: AppColors.textPrimary,
                              fontSize: 14,
                              height: 1.35,
                              fontWeight: FontWeight.w500,
                            ),
                          ),
                        ),
                        const SizedBox(width: 8),
                        const Icon(
                          Icons.arrow_outward_rounded,
                          size: 15,
                          color: AppColors.textTertiary,
                        ),
                      ],
                    ),
                  ),
                ),
              ),
          ],
        ),
      ),
    );
  }
}
