import 'dart:async';

import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../../../core/theme/app_colors.dart';
import '../../../core/theme/glass_card.dart';
import '../../../data/models/subscription_plan.dart';
import '../../../data/services/posthog_analytics_service.dart';
import '../../viewmodels/subscription_viewmodel.dart';

enum _PlanToggle { free, companion, pro }

enum _BillingPeriod { monthly, annual }

class PaywallScreen extends StatefulWidget {
  const PaywallScreen({super.key});

  @override
  State<PaywallScreen> createState() => _PaywallScreenState();
}

class _PaywallScreenState extends State<PaywallScreen> {
  _PlanToggle _activePlan = _PlanToggle.companion;
  _BillingPeriod _billingPeriod = _BillingPeriod.annual;
  final PageController _togglePageController = PageController(initialPage: 1);

  @override
  void initState() {
    super.initState();
    unawaited(context.read<PostHogAnalyticsService>().trackEvent('paywall_viewed'));
  }

  @override
  void dispose() {
    _togglePageController.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final activePricing = _pricingForPlan(_activePlan);

    return Scaffold(
      backgroundColor: Colors.transparent,
      body: AmbientBackground(
        child: SafeArea(
          child: Column(
            children: [
              Padding(
                padding: const EdgeInsets.symmetric(horizontal: 20, vertical: 12),
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
                  padding: const EdgeInsets.fromLTRB(24, 4, 24, 32),
                  child: Consumer<SubscriptionViewModel>(
                    builder: (context, vm, _) {
                      return Column(
                        crossAxisAlignment: CrossAxisAlignment.stretch,
                        children: [
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
                            '7-day free trial. Cancel anytime.',
                            style: TextStyle(
                              color: AppColors.textTertiary,
                              fontSize: 14,
                            ),
                            textAlign: TextAlign.center,
                          ),
                          const SizedBox(height: 24),

                          // Free / Companion / Pro toggle
                          _PlanToggleSwitch(
                            selected: _activePlan,
                            onChanged: (p) {
                              setState(() => _activePlan = p);
                              _togglePageController.animateToPage(
                                _planIndex(p),
                                duration: const Duration(milliseconds: 300),
                                curve: Curves.easeInOut,
                              );
                            },
                          ),
                          const SizedBox(height: 24),

                          // Feature list driven by the same PageController so
                          // swiping the content area updates the toggle pill too
                          SizedBox(
                            height: 350,
                            child: PageView(
                              controller: _togglePageController,
                              onPageChanged: (index) {
                                setState(() {
                                  _activePlan = _planFromIndex(index);
                                });
                              },
                              children: const [
                                _FeatureList(plan: _PlanToggle.free),
                                _FeatureList(plan: _PlanToggle.companion),
                                _FeatureList(plan: _PlanToggle.pro),
                              ],
                            ),
                          ),
                          const SizedBox(height: 24),

                          // Side-by-side billing cards
                          Row(
                            crossAxisAlignment: CrossAxisAlignment.start,
                            children: [
                              Expanded(
                                child: _BillingCard(
                                  period: _BillingPeriod.monthly,
                                  selected: _billingPeriod == _BillingPeriod.monthly,
                                  enabled: _activePlan != _PlanToggle.free,
                                  pricing: activePricing,
                                  onTap: _activePlan != _PlanToggle.free
                                      ? () => setState(() => _billingPeriod = _BillingPeriod.monthly)
                                      : null,
                                ),
                              ),
                              const SizedBox(width: 12),
                              Expanded(
                                child: _BillingCard(
                                  period: _BillingPeriod.annual,
                                  selected: _billingPeriod == _BillingPeriod.annual,
                                  enabled: _activePlan != _PlanToggle.free,
                                  pricing: activePricing,
                                  onTap: _activePlan != _PlanToggle.free
                                      ? () => setState(() => _billingPeriod = _BillingPeriod.annual)
                                      : null,
                                ),
                              ),
                            ],
                          ),
                          const SizedBox(height: 28),

                          // CTA
                          if (_activePlan != _PlanToggle.free) ...[
                            _CtaButton(
                              label: _activePlan == _PlanToggle.pro
                                  ? 'Start 7-day trial · Pro'
                                  : 'Start 7-day trial · Companion',
                              isLoading: vm.isLoading,
                              onTap: () => _onSubscribe(context, vm),
                            ),
                          ] else ...[
                            _GhostButton(
                              label: 'Continue with Free',
                              onTap: () => Navigator.pop(context),
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

  Future<void> _onSubscribe(BuildContext context, SubscriptionViewModel vm) async {
    final tier = _activePlan == _PlanToggle.pro
        ? SubscriptionTier.pro
        : SubscriptionTier.companion;
    final tierLabel = _activePlan == _PlanToggle.pro ? 'Pro' : 'Companion';
    final annual = _billingPeriod == _BillingPeriod.annual;

    unawaited(vm.captureInterest(tier: tier, annual: annual));

    if (!context.mounted) return;
    await showDialog<void>(
      context: context,
      barrierDismissible: true,
      builder: (ctx) => _InterestCapturedDialog(
        tierLabel: tierLabel,
        period: annual ? 'yearly' : 'monthly',
      ),
    );
  }

  static int _planIndex(_PlanToggle plan) {
    switch (plan) {
      case _PlanToggle.free:
        return 0;
      case _PlanToggle.companion:
        return 1;
      case _PlanToggle.pro:
        return 2;
    }
  }

  static _PlanToggle _planFromIndex(int index) {
    switch (index) {
      case 0:
        return _PlanToggle.free;
      case 2:
        return _PlanToggle.pro;
      default:
        return _PlanToggle.companion;
    }
  }
}

// Per-plan pricing

class _PlanPricing {
  final String symbol;
  final String monthly;
  final String annual;
  final String monthlyEquivalent;

  const _PlanPricing({
    required this.symbol,
    required this.monthly,
    required this.annual,
    required this.monthlyEquivalent,
  });
}

const _companionPricing = _PlanPricing(
  symbol: '\$',
  monthly: '19.99',
  annual: '191',
  monthlyEquivalent: '15.92',
);

const _proPricing = _PlanPricing(
  symbol: '\$',
  monthly: '34.99',
  annual: '335',
  monthlyEquivalent: '27.92',
);

_PlanPricing _pricingForPlan(_PlanToggle plan) {
  if (plan == _PlanToggle.pro) return _proPricing;
  return _companionPricing;
}

// Free / Companion / Pro toggle

class _PlanToggleSwitch extends StatelessWidget {
  final _PlanToggle selected;
  final ValueChanged<_PlanToggle> onChanged;

  const _PlanToggleSwitch({
    required this.selected,
    required this.onChanged,
  });

  @override
  Widget build(BuildContext context) {
    return Container(
      height: 52,
      decoration: BoxDecoration(
        color: AppColors.glassWhiteFill,
        borderRadius: BorderRadius.circular(26),
        border: Border.all(color: AppColors.glassBorderDim),
      ),
      padding: const EdgeInsets.all(4),
      child: Row(
        children: [
          _ToggleSegment(
            label: 'Free',
            isSelected: selected == _PlanToggle.free,
            onTap: () => onChanged(_PlanToggle.free),
          ),
          _ToggleSegment(
            label: 'Companion',
            isSelected: selected == _PlanToggle.companion,
            onTap: () => onChanged(_PlanToggle.companion),
          ),
          _ToggleSegment(
            label: 'Pro',
            isSelected: selected == _PlanToggle.pro,
            onTap: () => onChanged(_PlanToggle.pro),
          ),
        ],
      ),
    );
  }
}

class _ToggleSegment extends StatelessWidget {
  final String label;
  final bool isSelected;
  final VoidCallback onTap;

  const _ToggleSegment({
    required this.label,
    required this.isSelected,
    required this.onTap,
  });

  @override
  Widget build(BuildContext context) {
    return Expanded(
      child: GestureDetector(
        onTap: onTap,
        child: AnimatedContainer(
          duration: const Duration(milliseconds: 220),
          curve: Curves.easeOut,
          decoration: BoxDecoration(
            color: isSelected ? AppColors.accent : Colors.transparent,
            borderRadius: BorderRadius.circular(22),
          ),
          alignment: Alignment.center,
          child: Text(
            label,
            style: TextStyle(
              color: isSelected ? Colors.white : AppColors.textTertiary,
              fontSize: 14,
              fontWeight: isSelected ? FontWeight.w600 : FontWeight.w400,
            ),
          ),
        ),
      ),
    );
  }
}

// Feature list

class _FeatureItem {
  final IconData icon;
  final String text;
  final bool included;

  const _FeatureItem({
    required this.icon,
    required this.text,
    required this.included,
  });
}

const _freeFeatureItems = [
  _FeatureItem(
    icon: Icons.record_voice_over_outlined,
    text: '15 voice minutes per month',
    included: true,
  ),
  _FeatureItem(
    icon: Icons.chat_bubble_outline_rounded,
    text: '50 chat messages per month',
    included: true,
  ),
  _FeatureItem(
    icon: Icons.notifications_outlined,
    text: 'Basic reminders',
    included: true,
  ),
  _FeatureItem(
    icon: Icons.extension_outlined,
    text: 'Calendar and more agents',
    included: false,
  ),
  _FeatureItem(
    icon: Icons.memory_rounded,
    text: 'Aura memory profile',
    included: false,
  ),
];

const _companionFeatureItems = [
  _FeatureItem(
    icon: Icons.record_voice_over_outlined,
    text: '120 voice minutes per month',
    included: true,
  ),
  _FeatureItem(
    icon: Icons.chat_bubble_outline_rounded,
    text: '500 chat messages per month',
    included: true,
  ),
  _FeatureItem(
    icon: Icons.calendar_today_outlined,
    text: 'Calendar + reminders unlimited',
    included: true,
  ),
  _FeatureItem(
    icon: Icons.memory_rounded,
    text: 'Unlimited Aura memory',
    included: true,
  ),
];

const _proFeatureItems = [
  _FeatureItem(
    icon: Icons.record_voice_over_outlined,
    text: '400 voice minutes per month',
    included: true,
  ),
  _FeatureItem(
    icon: Icons.all_inclusive_rounded,
    text: 'Unlimited chat',
    included: true,
  ),
  _FeatureItem(
    icon: Icons.bolt_outlined,
    text: 'Priority voice + premium TTS',
    included: true,
  ),
  _FeatureItem(
    icon: Icons.tune_rounded,
    text: 'Custom interest tuning for notifications',
    included: true,
  ),
  _FeatureItem(
    icon: Icons.workspace_premium_outlined,
    text: 'Everything in Companion',
    included: true,
  ),
];

class _FeatureList extends StatelessWidget {
  final _PlanToggle plan;

  const _FeatureList({required this.plan});

  @override
  Widget build(BuildContext context) {
    final items = switch (plan) {
      _PlanToggle.free => _freeFeatureItems,
      _PlanToggle.companion => _companionFeatureItems,
      _PlanToggle.pro => _proFeatureItems,
    };

    return FauxGlassCard(
      borderRadius: 18,
      padding: const EdgeInsets.symmetric(horizontal: 18, vertical: 16),
      child: Column(
        children: [
          for (int i = 0; i < items.length; i++) ...[
            if (i > 0)
              Padding(
                padding: const EdgeInsets.symmetric(vertical: 10),
                child: Divider(
                  height: 1,
                  color: AppColors.glassBorderDim,
                ),
              ),
            _FeatureRow(item: items[i]),
          ],
        ],
      ),
    );
  }
}

class _FeatureRow extends StatelessWidget {
  final _FeatureItem item;

  const _FeatureRow({required this.item});

  @override
  Widget build(BuildContext context) {
    return Row(
      children: [
        Container(
          width: 32,
          height: 32,
          decoration: BoxDecoration(
            color: item.included
                ? AppColors.accent.withValues(alpha: 0.12)
                : AppColors.glassWhiteFill,
            borderRadius: BorderRadius.circular(9),
          ),
          child: Icon(
            item.icon,
            color: item.included ? AppColors.accent : AppColors.textDisabled,
            size: 16,
          ),
        ),
        const SizedBox(width: 12),
        Expanded(
          child: Text(
            item.text,
            style: TextStyle(
              color: item.included ? AppColors.textSecondary : AppColors.textDisabled,
              fontSize: 14,
            ),
          ),
        ),
        const SizedBox(width: 8),
        Icon(
          item.included ? Icons.check_rounded : Icons.lock_outline_rounded,
          color: item.included ? AppColors.accent : AppColors.textDisabled,
          size: 15,
        ),
      ],
    );
  }
}

// Billing cards

class _BillingCard extends StatelessWidget {
  final _BillingPeriod period;
  final bool selected;
  final bool enabled;
  final _PlanPricing pricing;
  final VoidCallback? onTap;

  const _BillingCard({
    required this.period,
    required this.selected,
    required this.enabled,
    required this.pricing,
    required this.onTap,
  });

  @override
  Widget build(BuildContext context) {
    final isAnnual = period == _BillingPeriod.annual;
    final isActive = selected && enabled;

    return GestureDetector(
      onTap: onTap,
      child: AnimatedOpacity(
        duration: const Duration(milliseconds: 200),
        opacity: enabled ? 1.0 : 0.38,
        child: AnimatedContainer(
          duration: const Duration(milliseconds: 220),
          curve: Curves.easeOut,
          padding: const EdgeInsets.all(14),
          decoration: BoxDecoration(
            gradient: LinearGradient(
              begin: Alignment.topLeft,
              end: Alignment.bottomRight,
              colors: isActive
                  ? [
                      AppColors.accent.withValues(alpha: 0.20),
                      AppColors.accent.withValues(alpha: 0.08),
                    ]
                  : [
                      const Color(0x0F2B2A26),
                      const Color(0x082B2A26),
                    ],
            ),
            borderRadius: BorderRadius.circular(18),
            border: Border.all(
              color: isActive
                  ? AppColors.accent.withValues(alpha: 0.6)
                  : AppColors.glassBorderDim,
              width: isActive ? 1.5 : 1,
            ),
          ),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Row(
                children: [
                  Text(
                    isAnnual ? 'Yearly' : 'Monthly',
                    style: TextStyle(
                      color: isActive ? AppColors.accent : AppColors.textPrimary,
                      fontSize: 13,
                      fontWeight: FontWeight.w600,
                    ),
                  ),
                  if (isAnnual) ...[
                    const SizedBox(width: 5),
                    Container(
                      padding: const EdgeInsets.symmetric(horizontal: 5, vertical: 2),
                      decoration: BoxDecoration(
                        color: AppColors.accent.withValues(alpha: 0.15),
                        borderRadius: BorderRadius.circular(5),
                      ),
                      child: const Text(
                        'Save 20%',
                        style: TextStyle(
                          color: AppColors.accent,
                          fontSize: 9,
                          fontWeight: FontWeight.w700,
                        ),
                      ),
                    ),
                  ],
                ],
              ),
              const SizedBox(height: 10),
              if (isAnnual) ...[
                Text(
                  '${pricing.symbol}${pricing.monthly}/mo',
                  style: const TextStyle(
                    color: AppColors.textDisabled,
                    fontSize: 12,
                    decoration: TextDecoration.lineThrough,
                    decorationColor: AppColors.textDisabled,
                  ),
                ),
                const SizedBox(height: 2),
                Text(
                  '${pricing.symbol}${pricing.monthlyEquivalent}/mo',
                  style: TextStyle(
                    color: isActive ? AppColors.accent : AppColors.textPrimary,
                    fontSize: 20,
                    fontWeight: FontWeight.w700,
                    letterSpacing: -0.5,
                  ),
                ),
                const SizedBox(height: 4),
                Text(
                  'Billed ${pricing.symbol}${pricing.annual} per year',
                  style: const TextStyle(
                    color: AppColors.textTertiary,
                    fontSize: 11,
                  ),
                ),
              ] else ...[
                Text(
                  '${pricing.symbol}${pricing.monthly}/mo',
                  style: TextStyle(
                    color: isActive ? AppColors.accent : AppColors.textPrimary,
                    fontSize: 20,
                    fontWeight: FontWeight.w700,
                    letterSpacing: -0.5,
                  ),
                ),
                const SizedBox(height: 4),
                const Text(
                  'billed monthly',
                  style: TextStyle(
                    color: AppColors.textTertiary,
                    fontSize: 11,
                  ),
                ),
              ],
            ],
          ),
        ),
      ),
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
            gradient: LinearGradient(
              colors: [AppColors.accent, AppColors.accentDark],
            ),
            borderRadius: BorderRadius.circular(22),
            boxShadow: [
              BoxShadow(
                color: AppColors.accent.withValues(alpha: 0.38),
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
                    child: CircularProgressIndicator(color: Colors.white, strokeWidth: 2),
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

// Ghost button for Free plan

class _GhostButton extends StatelessWidget {
  final String label;
  final VoidCallback onTap;

  const _GhostButton({required this.label, required this.onTap});

  @override
  Widget build(BuildContext context) {
    return GestureDetector(
      onTap: onTap,
      child: Container(
        height: 54,
        decoration: BoxDecoration(
          borderRadius: BorderRadius.circular(22),
          border: Border.all(color: AppColors.glassBorderLight),
        ),
        child: Center(
          child: Text(
            label,
            style: const TextStyle(
              color: AppColors.textSecondary,
              fontSize: 16,
              fontWeight: FontWeight.w500,
            ),
          ),
        ),
      ),
    );
  }
}

// Acknowledgement dialog shown during beta in place of real IAP

class _InterestCapturedDialog extends StatelessWidget {
  final String tierLabel;
  final String period;

  const _InterestCapturedDialog({
    required this.tierLabel,
    required this.period,
  });

  @override
  Widget build(BuildContext context) {
    return AlertDialog(
      backgroundColor: AppColors.deepBackground,
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(20),
        side: BorderSide(color: AppColors.glassBorderLight),
      ),
      title: const Text(
        'Thanks for your interest!',
        style: TextStyle(
          color: AppColors.textPrimary,
          fontSize: 18,
          fontWeight: FontWeight.w700,
        ),
      ),
      content: Text(
        "Payments aren't live yet. We're shipping the final pieces. "
        "We saved your interest in $tierLabel ($period) and you'll be the "
        "first to know the moment it goes live.",
        style: const TextStyle(
          color: AppColors.textSecondary,
          fontSize: 14,
          height: 1.4,
        ),
      ),
      actions: [
        TextButton(
          onPressed: () => Navigator.of(context).pop(),
          child: const Text(
            'Got it',
            style: TextStyle(
              color: AppColors.accent,
              fontWeight: FontWeight.w600,
            ),
          ),
        ),
      ],
    );
  }
}
