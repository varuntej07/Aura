import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../../../core/theme/app_colors.dart';
import '../../../core/theme/glass_card.dart';
import '../../../data/models/subscription_plan.dart';
import '../../viewmodels/subscription_viewmodel.dart';

class PaywallScreen extends StatefulWidget {
  const PaywallScreen({super.key});

  @override
  State<PaywallScreen> createState() => _PaywallScreenState();
}

class _PaywallScreenState extends State<PaywallScreen> {
  _Plan _selected = _Plan.annual;

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: Colors.transparent,
      body: AmbientBackground(
        child: SafeArea(
          child: Column(
            children: [
              // Top bar 
              Padding(
                padding: const EdgeInsets.symmetric(
                    horizontal: 20, vertical: 12),
                child: Align(
                  alignment: Alignment.centerLeft,
                  child: GlassIconButton(
                    icon: Icons.close_rounded,
                    onTap: () => Navigator.pop(context),
                    iconSize: 18,
                  ),
                ),
              ),

              Expanded(
                child: SingleChildScrollView(
                  padding: const EdgeInsets.fromLTRB(24, 8, 24, 32),
                  child: Consumer<SubscriptionViewModel>(
                    builder: (context, vm, _) {
                      return Column(
                        crossAxisAlignment: CrossAxisAlignment.stretch,
                        children: [
                          // ── Hero ─────────────────────────────────────────
                          const SizedBox(height: 8),
                          GlassCard(
                            borderRadius: 32,
                            padding: const EdgeInsets.all(18),
                            borderColor:
                                AppColors.accent.withValues(alpha: 0.4),
                            child: const Icon(Icons.mic_rounded,
                                color: AppColors.accent, size: 40),
                          ),
                          const SizedBox(height: 20),
                          const Text(
                            'Unlock Aura',
                            style: TextStyle(
                              color: AppColors.textPrimary,
                              fontSize: 30,
                              fontWeight: FontWeight.w700,
                              letterSpacing: -1,
                            ),
                            textAlign: TextAlign.center,
                          ),
                          const SizedBox(height: 6),
                          const Text(
                            '14-day free trial · Cancel anytime',
                            style: TextStyle(
                              color: AppColors.textTertiary,
                              fontSize: 14,
                            ),
                            textAlign: TextAlign.center,
                          ),

                          const SizedBox(height: 32),

                          // Free tier 
                          _PlanCard(
                            plan: _Plan.free,
                            selected: _selected == _Plan.free,
                            onTap: () =>
                                setState(() => _selected = _Plan.free),
                          ),
                          const SizedBox(height: 10),

                          // Monthly
                          _PlanCard(
                            plan: _Plan.monthly,
                            selected: _selected == _Plan.monthly,
                            product: vm.products
                                .where((p) =>
                                    p.id ==
                                    SubscriptionProductIds.starterMonthly)
                                .firstOrNull,
                            onTap: () =>
                                setState(() => _selected = _Plan.monthly),
                          ),
                          const SizedBox(height: 10),

                          // Annual
                          _PlanCard(
                            plan: _Plan.annual,
                            selected: _selected == _Plan.annual,
                            product: vm.products
                                .where((p) =>
                                    p.id ==
                                    SubscriptionProductIds.starterAnnual)
                                .firstOrNull,
                            onTap: () =>
                                setState(() => _selected = _Plan.annual),
                          ),

                          const SizedBox(height: 28),

                          // Feature list
                          _FeatureRow(
                            icon: Icons.memory_rounded,
                            text:
                                'Aura remembers you across every session',
                          ),
                          const SizedBox(height: 10),
                          _FeatureRow(
                            icon: Icons.notifications_active_outlined,
                            text:
                                'Proactive briefings and smart nudges',
                          ),
                          const SizedBox(height: 10),
                          _FeatureRow(
                            icon: Icons.record_voice_over_outlined,
                            text: 'Natural LiveKit voice with Buddy',
                          ),
                          const SizedBox(height: 10),
                          _FeatureRow(
                            icon: Icons.extension_outlined,
                            text:
                                'Calendar, nutrition, sports, and more agents',
                          ),

                          const SizedBox(height: 32),

                          // CTA 
                          if (_selected != _Plan.free) ...[
                            _CtaButton(
                              label: vm.isTrialActive
                                  ? 'Continue with ${_selected.label}'
                                  : 'Start 14-day free trial',
                              isLoading: vm.isLoading,
                              onTap: () => _purchase(context, vm),
                            ),
                            const SizedBox(height: 12),
                            GestureDetector(
                              onTap: vm.isLoading
                                  ? null
                                  : vm.restorePurchases,
                              child: const Center(
                                child: Text(
                                  'Restore purchases',
                                  style: TextStyle(
                                    color: AppColors.textTertiary,
                                    fontSize: 13,
                                    decoration: TextDecoration.underline,
                                  ),
                                ),
                              ),
                            ),
                          ] else ...[
                            FauxGlassCard(
                              borderRadius: 16,
                              padding: const EdgeInsets.symmetric(
                                  vertical: 16),
                              child: const Center(
                                child: Text(
                                  'Continue with Free',
                                  style: TextStyle(
                                    color: AppColors.textSecondary,
                                    fontSize: 16,
                                    fontWeight: FontWeight.w500,
                                  ),
                                ),
                              ),
                            ),
                          ],

                          if (vm.errorMessage != null) ...[
                            const SizedBox(height: 12),
                            Text(
                              vm.errorMessage!,
                              style: const TextStyle(
                                color: AppColors.error,
                                fontSize: 13,
                              ),
                              textAlign: TextAlign.center,
                            ),
                          ],

                          const SizedBox(height: 16),
                          const Center(
                            child: Text(
                              'Prices shown in local currency · Billed by Apple or Google',
                              style: TextStyle(
                                color: AppColors.textTertiary,
                                fontSize: 11,
                              ),
                              textAlign: TextAlign.center,
                            ),
                          ),
                        ],
                      );
                    },
                  ),
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }

  void _purchase(BuildContext context, SubscriptionViewModel vm) {
    final annual = _selected == _Plan.annual;
    vm.purchaseStarter(annual: annual);
  }
}

// Plan enum

enum _Plan { free, monthly, annual }

extension _PlanExt on _Plan {
  String get label {
    return switch (this) {
      _Plan.free => 'Free',
      _Plan.monthly => 'Monthly',
      _Plan.annual => 'Annual',
    };
  }

  String get fallbackPrice {
    return switch (this) {
      _Plan.free => 'Free forever',
      _Plan.monthly => '₹499 / month',
      _Plan.annual => '₹3,999 / year',
    };
  }

  String? get badge {
    return switch (this) {
      _Plan.annual => 'Save 33%',
      _ => null,
    };
  }

  List<String> get bullets {
    return switch (this) {
      _Plan.free => [
          '20 messages/day with Buddy',
          'Basic reminders',
          '7-day chat history',
        ],
      _Plan.monthly || _Plan.annual => [
          'Unlimited Buddy chat',
          'All agents unlocked',
          'Voice + proactive briefings',
          'Full Aura memory profile',
          'Unlimited chat history',
        ],
    };
  }
}

// Plan card

class _PlanCard extends StatelessWidget {
  final _Plan plan;
  final bool selected;
  final dynamic product;
  final VoidCallback onTap;

  const _PlanCard({
    required this.plan,
    required this.selected,
    required this.onTap,
    this.product,
  });

  @override
  Widget build(BuildContext context) {
    final priceStr =
        product?.price as String? ?? plan.fallbackPrice;

    return GestureDetector(
      onTap: onTap,
      child: AnimatedContainer(
        duration: const Duration(milliseconds: 200),
        curve: Curves.easeOut,
        decoration: BoxDecoration(
          gradient: LinearGradient(
            begin: Alignment.topLeft,
            end: Alignment.bottomRight,
            colors: selected
                ? [
                    AppColors.accent.withValues(alpha: 0.20),
                    AppColors.accent.withValues(alpha: 0.08),
                  ]
                : [
                    const Color(0x14FFFFFF),
                    const Color(0x08FFFFFF),
                  ],
          ),
          borderRadius: BorderRadius.circular(18),
          border: Border.all(
            color: selected
                ? AppColors.accent.withValues(alpha: 0.6)
                : AppColors.glassBorderDim,
            width: selected ? 1.5 : 1,
          ),
        ),
        padding: const EdgeInsets.fromLTRB(16, 14, 16, 14),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Row(
                        children: [
                          Text(
                            plan.label,
                            style: TextStyle(
                              color: selected
                                  ? AppColors.accent
                                  : AppColors.textPrimary,
                              fontSize: 16,
                              fontWeight: FontWeight.w600,
                            ),
                          ),
                          if (plan.badge != null) ...[
                            const SizedBox(width: 8),
                            Container(
                              padding: const EdgeInsets.symmetric(
                                  horizontal: 8, vertical: 3),
                              decoration: BoxDecoration(
                                color: AppColors.accent
                                    .withValues(alpha: 0.15),
                                borderRadius: BorderRadius.circular(6),
                              ),
                              child: Text(
                                plan.badge!,
                                style: const TextStyle(
                                  color: AppColors.accent,
                                  fontSize: 11,
                                  fontWeight: FontWeight.w600,
                                ),
                              ),
                            ),
                          ],
                        ],
                      ),
                      const SizedBox(height: 2),
                      Text(
                        priceStr,
                        style: const TextStyle(
                          color: AppColors.textSecondary,
                          fontSize: 13,
                        ),
                      ),
                    ],
                  ),
                ),
                AnimatedContainer(
                  duration: const Duration(milliseconds: 200),
                  width: 22,
                  height: 22,
                  decoration: BoxDecoration(
                    shape: BoxShape.circle,
                    color: selected ? AppColors.accent : Colors.transparent,
                    border: Border.all(
                      color: selected
                          ? AppColors.accent
                          : AppColors.glassBorderLight,
                      width: 1.5,
                    ),
                  ),
                  child: selected
                      ? const Icon(Icons.check_rounded,
                          color: Colors.white, size: 14)
                      : null,
                ),
              ],
            ),
            if (selected) ...[
              const SizedBox(height: 12),
              const Divider(color: AppColors.glassBorderDim, height: 1),
              const SizedBox(height: 10),
              ...plan.bullets.map(
                (b) => Padding(
                  padding: const EdgeInsets.only(bottom: 5),
                  child: Row(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      const Icon(Icons.check_circle_outline_rounded,
                          color: AppColors.accent, size: 15),
                      const SizedBox(width: 8),
                      Expanded(
                        child: Text(
                          b,
                          style: const TextStyle(
                            color: AppColors.textSecondary,
                            fontSize: 13,
                          ),
                        ),
                      ),
                    ],
                  ),
                ),
              ),
            ],
          ],
        ),
      ),
    );
  }
}

// Feature row

class _FeatureRow extends StatelessWidget {
  final IconData icon;
  final String text;

  const _FeatureRow({required this.icon, required this.text});

  @override
  Widget build(BuildContext context) {
    return Row(
      children: [
        Container(
          width: 32,
          height: 32,
          decoration: BoxDecoration(
            color: AppColors.accent.withValues(alpha: 0.12),
            borderRadius: BorderRadius.circular(9),
          ),
          child: Icon(icon, color: AppColors.accent, size: 16),
        ),
        const SizedBox(width: 12),
        Expanded(
          child: Text(
            text,
            style: const TextStyle(
              color: AppColors.textSecondary,
              fontSize: 14,
            ),
          ),
        ),
      ],
    );
  }
}

// CTA button

class _CtaButton extends StatelessWidget {
  final String label;
  final VoidCallback onTap;
  final bool isLoading;

  const _CtaButton({
    required this.label,
    required this.onTap,
    this.isLoading = false,
  });

  @override
  Widget build(BuildContext context) {
    return GestureDetector(
      onTap: isLoading ? null : onTap,
      child: AnimatedOpacity(
        opacity: isLoading ? 0.6 : 1.0,
        duration: const Duration(milliseconds: 150),
        child: Container(
          height: 54,
          decoration: BoxDecoration(
            gradient: const LinearGradient(
              colors: [AppColors.accent, AppColors.accentDark],
            ),
            borderRadius: BorderRadius.circular(16),
            boxShadow: [
              BoxShadow(
                color: AppColors.accent.withValues(alpha: 0.40),
                blurRadius: 20,
                offset: const Offset(0, 6),
              ),
            ],
          ),
          child: Center(
            child: isLoading
                ? const SizedBox(
                    width: 22,
                    height: 22,
                    child: CircularProgressIndicator(
                        color: Colors.white, strokeWidth: 2),
                  )
                : Text(
                    label,
                    style: const TextStyle(
                      color: Colors.white,
                      fontSize: 16,
                      fontWeight: FontWeight.w600,
                    ),
                  ),
          ),
        ),
      ),
    );
  }
}
