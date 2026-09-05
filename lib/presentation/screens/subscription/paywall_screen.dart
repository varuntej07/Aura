import 'dart:async';

import 'package:flutter/material.dart';
import 'package:provider/provider.dart';
import 'package:url_launcher/url_launcher.dart';

import '../../../core/theme/app_colors.dart';
import '../../../core/theme/glass_card.dart';
import '../../../data/models/subscription_plan.dart';
import '../../../data/services/app_feedback_service.dart';
import '../../../data/services/notification_service.dart';
import '../../../data/services/posthog_analytics_service.dart';
import '../../../data/services/store_purchase_service.dart';
import '../../viewmodels/auth_viewmodel.dart';
import '../../viewmodels/subscription_viewmodel.dart';
import '../../widgets/shimmer_sweep.dart';

enum _PlanToggle { free, companion, pro }

enum _BillingPeriod { monthly, annual }

class PaywallScreen extends StatefulWidget {
  /// Set when the paywall was opened by tapping a trial-lifecycle notification
  /// (3-days-left warning or trial-ended notice); drives contextual copy instead
  /// of the generic entry-point subtitle. Null for every other entry point
  /// (Settings, in-chat upsell, etc).
  final TrialTapPayload? trialReason;

  const PaywallScreen({super.key, this.trialReason});

  @override
  State<PaywallScreen> createState() => _PaywallScreenState();
}

class _PaywallScreenState extends State<PaywallScreen>
    with WidgetsBindingObserver {
  _PlanToggle _activePlan = _PlanToggle.companion;
  _BillingPeriod _billingPeriod = _BillingPeriod.annual;

  /// Whether this visit already registered upgrade interest. Session-scoped on
  /// purpose: the write is idempotent per user and re-tapping costs nothing, so
  /// this only exists to give immediate feedback rather than to enforce a rule.
  bool _interestRegistered = false;
  final PageController _togglePageController = PageController(initialPage: 1);

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    unawaited(
      context.read<PostHogAnalyticsService>().trackEvent(
        'paywall_viewed',
        properties: {'trigger': widget.trialReason?.variant ?? 'direct'},
      ),
    );
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    _togglePageController.dispose();
    super.dispose();
  }

  /// The user pays in the system browser, so the unlock moment is usually
  /// "came back to the app": refetch entitlement on every resume while the
  /// paywall is up (one backend read, and the FCM push covers the rest).
  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state == AppLifecycleState.resumed && mounted) {
      unawaited(context.read<SubscriptionViewModel>().refreshEntitlement());
    }
  }

  /// The tier the account actually PAYS for, or null when nothing is owned.
  ///
  /// Deliberately NOT [SubscriptionViewModel.currentTier]: that is the
  /// backend's *effective* tier, which resolves to pro for the whole free
  /// trial, so every trial user would be marked as owning Pro. The
  /// `effectiveTier == tier` check additionally drops lapsed subscribers,
  /// whose purchased tier is still pro while their access has already
  /// resolved to free.
  _PlanToggle? _ownedPlan(SubscriptionViewModel vm) {
    final entitlement = vm.entitlement;
    if (entitlement == null || vm.isTrialActive) return null;
    if (!entitlement.isPaid || entitlement.effectiveTier != entitlement.tier) {
      return null;
    }
    return switch (entitlement.tier) {
      SubscriptionTier.pro => _PlanToggle.pro,
      SubscriptionTier.companion => _PlanToggle.companion,
      SubscriptionTier.free => null,
    };
  }

  /// Warm, contextual subtitle when arriving from a trial-lifecycle notification
  /// tap, otherwise the generic entry-point copy.
  String get _subtitle => switch (widget.trialReason?.variant) {
    '3d_warning' => "Your trial wraps up in 3 days. Let's keep this going.",
    'expired' =>
      "Your trial's over, but Buddy's not going anywhere. Pick back up anytime.",
    _ => "$kTrialDurationDays days free while we're in beta",
  };

  @override
  Widget build(BuildContext context) {
    // On iOS StoreKit is the seller and the App Store's own localized price is
    // what must be displayed, so the USD constants are used only off-iOS.
    final store = context.watch<StorePurchaseService>();
    final storePricing = StorePurchaseService.isStorePlatform
        ? _storePricingForPlan(store, _activePlan)
        : null;
    final activePricing = storePricing ?? _pricingForPlan(_activePlan);

    return Scaffold(
      backgroundColor: Colors.transparent,
      body: AmbientBackground(
        child: SafeArea(
          child: Column(
            children: [
              Padding(
                padding: const EdgeInsets.symmetric(
                  horizontal: 20,
                  vertical: 12,
                ),
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
                // minHeight lets the Column centre itself in the leftover
                // space when the content is shorter than the screen, while
                // still scrolling normally when it is not.
                child: LayoutBuilder(
                  builder: (context, constraints) => SingleChildScrollView(
                    padding: const EdgeInsets.fromLTRB(24, 4, 24, 32),
                    child: ConstrainedBox(
                      constraints: BoxConstraints(
                        minHeight: constraints.maxHeight - 36,
                      ),
                      child: Consumer<SubscriptionViewModel>(
                        builder: (context, vm, _) {
                          final storeCanPurchase =
                              storePricing != null &&
                              store.isAvailable &&
                              !vm.isPaid;
                          return Column(
                            mainAxisAlignment: MainAxisAlignment.center,
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
                              Text(
                                _subtitle,
                                style: const TextStyle(
                                  color: AppColors.textTertiary,
                                  fontSize: 14,
                                ),
                                textAlign: TextAlign.center,
                              ),
                              const SizedBox(height: 24),

                              // Free / Companion / Pro toggle. `owned` marks the
                              // tier the account actually pays for, which is not
                              // the same thing as the segment being browsed.
                              _PlanToggleSwitch(
                                selected: _activePlan,
                                owned: _ownedPlan(vm),
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
                              Stack(
                                fit: StackFit.passthrough,
                                children: [
                                  // Layout-only ghosts. A Stack sizes to its
                                  // tallest non-positioned child, so these give the
                                  // PageView (which cannot shrink-wrap) exactly the
                                  // height of the longest plan at the current width
                                  // and text scale. That is what removes the dead
                                  // space a hardcoded height left under the last
                                  // row, without a constant that goes stale when a
                                  // feature is added. Visibility rather than
                                  // Opacity: it also excludes them from hit testing
                                  // and from semantics, so the list is not read out
                                  // three times.
                                  for (final plan in _PlanToggle.values)
                                    Visibility(
                                      visible: false,
                                      maintainSize: true,
                                      maintainAnimation: true,
                                      maintainState: true,
                                      child: _FeatureList(plan: plan),
                                    ),
                                  Positioned.fill(
                                    child: PageView(
                                      controller: _togglePageController,
                                      onPageChanged: (index) {
                                        setState(() {
                                          _activePlan = _planFromIndex(index);
                                        });
                                      },
                                      children: const [
                                        _FeatureList(plan: _PlanToggle.free),
                                        _FeatureList(
                                          plan: _PlanToggle.companion,
                                        ),
                                        _FeatureList(plan: _PlanToggle.pro),
                                      ],
                                    ),
                                  ),
                                ],
                              ),
                              const SizedBox(height: 24),

                              // Two sellers, one screen. Off iOS, purchase UI
                              // appears once the trial has ended and Dodo is
                              // configured, in every country. On iOS StoreKit sells
                              // and the products must stay reachable during the
                              // trial too, because App Review has to be able to
                              // exercise the in-app purchase to approve it.
                              if (vm.canPurchaseSubscription ||
                                  storeCanPurchase) ...[
                                // Side-by-side billing cards
                                Row(
                                  crossAxisAlignment: CrossAxisAlignment.start,
                                  children: [
                                    Expanded(
                                      child: _BillingCard(
                                        period: _BillingPeriod.monthly,
                                        selected:
                                            _billingPeriod ==
                                            _BillingPeriod.monthly,
                                        enabled:
                                            _activePlan != _PlanToggle.free,
                                        pricing: activePricing,
                                        onTap: _activePlan != _PlanToggle.free
                                            ? () => setState(
                                                () => _billingPeriod =
                                                    _BillingPeriod.monthly,
                                              )
                                            : null,
                                      ),
                                    ),
                                    const SizedBox(width: 12),
                                    Expanded(
                                      child: _BillingCard(
                                        period: _BillingPeriod.annual,
                                        selected:
                                            _billingPeriod ==
                                            _BillingPeriod.annual,
                                        enabled:
                                            _activePlan != _PlanToggle.free,
                                        pricing: activePricing,
                                        onTap: _activePlan != _PlanToggle.free
                                            ? () => setState(
                                                () => _billingPeriod =
                                                    _BillingPeriod.annual,
                                              )
                                            : null,
                                      ),
                                    ),
                                  ],
                                ),
                                const SizedBox(height: 28),

                                // CTA. On iOS this is a StoreKit purchase sheet;
                                // everywhere else checkout happens in the system
                                // browser and the device unlocks by push or refetch.
                                if (_activePlan != _PlanToggle.free) ...[
                                  _CtaButton(
                                    label: _ctaLabel(
                                      onStore: storeCanPurchase,
                                      plan: _activePlan,
                                    ),
                                    isLoading:
                                        vm.isLoading || store.isPurchasing,
                                    onTap: () => storeCanPurchase
                                        ? _onStoreSubscribe(context, store)
                                        : _onSubscribe(context, vm),
                                  ),
                                  // Both are required by App Review on any screen
                                  // that sells a subscription: someone who
                                  // reinstalled needs their plan back without
                                  // paying twice, and a subscriber must be able to
                                  // reach Apple's own cancel and manage sheet.
                                  if (storeCanPurchase) ...[
                                    const SizedBox(height: 12),
                                    _GhostButton(
                                      label: 'Restore Purchases',
                                      onTap: () => _onRestore(context, store),
                                    ),
                                    const SizedBox(height: 8),
                                    _GhostButton(
                                      label: 'Manage Subscription',
                                      onTap: _openManageSubscriptions,
                                    ),
                                  ],
                                ] else ...[
                                  _GhostButton(
                                    label: 'Continue with Free',
                                    onTap: () => Navigator.pop(context),
                                  ),
                                ],
                              ] else ...[
                                // Deliberate breathing room. With no purchase
                                // UI to fill it, the plan card and the bottom
                                // action sat stacked against each other.
                                const SizedBox(height: 32),
                                // "You're all set" told a healthy subscriber
                                // nothing. The card now renders only when it has
                                // something to say: a trial countdown, free-tier
                                // allowances, a pending cancellation, or — off
                                // iOS — the only pointer to where a subscription
                                // can be managed.
                                if (vm.isTrialActive ||
                                    !vm.isPaid ||
                                    vm.entitlement?.cancelAtPeriodEnd == true ||
                                    !vm.purchaseHandledOffPlatform) ...[
                                  _PlanStatusCard(vm: vm),
                                  const SizedBox(height: 20),
                                ],
                                // No purchase path yet: still inside the trial, or
                                // checkout is not configured. Capture the demand
                                // instead of showing a dead end. This is the only
                                // signal for how many people would pay early.
                                if (vm.showFreePlanStatus) ...[
                                  _UpgradeInterestButton(
                                    registered: _interestRegistered,
                                    onTap: () =>
                                        _registerUpgradeInterest(context),
                                  ),
                                ],
                                // A subscriber must always be able to reach
                                // Apple's cancel and manage sheet, including from
                                // the paid state where there is nothing to buy.
                                if (StorePurchaseService.isStorePlatform &&
                                    vm.isPaid) ...[
                                  _GhostButton(
                                    label: 'Manage Subscription',
                                    onTap: _openManageSubscriptions,
                                  ),
                                ],
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
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }

  /// Records that this user wants to pay but cannot yet.
  ///
  /// Reuses [AppFeedbackService] rather than adding a bespoke endpoint: it
  /// already pairs one Firestore write with one PostHog event, which is exactly
  /// the shape needed, and keeps interest capture on the same path as every
  /// other piece of user-volunteered signal.
  ///
  /// Optimistic: the button confirms immediately. A failed write costs a
  /// datapoint, and making someone watch a spinner to say "I'd pay you" is a
  /// worse trade than losing one record.
  Future<void> _registerUpgradeInterest(BuildContext context) async {
    if (_interestRegistered) return;
    setState(() => _interestRegistered = true);

    final uid = context.read<AuthViewModel>().user?.uid;
    if (uid == null) return;

    unawaited(
      context.read<AppFeedbackService>().submit(
        uid: uid,
        category: 'upgrade_interest',
        extraFields: {
          'tier_of_interest': _activePlan.name,
          'billing_period': _billingPeriod.name,
        },
        extraEventProperties: {
          'tier_of_interest': _activePlan.name,
          'billing_period': _billingPeriod.name,
        },
      ),
    );
  }

  static String _ctaLabel({required bool onStore, required _PlanToggle plan}) {
    final tier = plan == _PlanToggle.pro ? 'Pro' : 'Companion';
    // Off iOS the label says where payment happens, because the app is about to
    // hand the user to a browser. On iOS it must not, both because the sheet is
    // right here and because naming an outside destination is what App Review
    // reads as steering.
    return onStore ? 'Subscribe · $tier' : 'Upgrade on the web · $tier';
  }

  /// Apple's own subscription management sheet. The only outbound link allowed
  /// on this screen, and the one App Review expects to find.
  Future<void> _openManageSubscriptions() async {
    await launchUrl(
      Uri.parse('https://apps.apple.com/account/subscriptions'),
      mode: LaunchMode.externalApplication,
    );
  }

  Future<void> _onStoreSubscribe(
    BuildContext context,
    StorePurchaseService store,
  ) async {
    final tier = _activePlan == _PlanToggle.pro
        ? SubscriptionTier.pro
        : SubscriptionTier.companion;
    final annual = _billingPeriod == _BillingPeriod.annual;
    final product = store.productFor(tier, annual: annual);
    if (product == null) {
      _showMessage(context, "That plan isn't available right now.");
      return;
    }

    unawaited(
      context.read<PostHogAnalyticsService>().trackEvent(
        'checkout_opened',
        properties: {
          'tier': tier.name,
          'billing_period': annual ? 'yearly' : 'monthly',
          'seller': 'apple',
        },
      ),
    );

    // Everything after this arrives on the purchase stream, including the
    // backend verification and the entitlement refetch, so there is nothing to
    // await here beyond StoreKit accepting the request.
    await store.buy(product);
    if (!context.mounted) return;
    final error = store.errorMessage;
    if (error != null) _showMessage(context, error);
  }

  Future<void> _onRestore(
    BuildContext context,
    StorePurchaseService store,
  ) async {
    await store.restore();
    if (!context.mounted) return;
    // Restoring finds nothing far more often than it fails, so say what
    // happened rather than leaving the button looking broken.
    _showMessage(
      context,
      store.errorMessage ??
          (context.read<SubscriptionViewModel>().isPaid
              ? 'Your plan is back.'
              : 'No previous purchase found on this Apple ID.'),
    );
  }

  void _showMessage(BuildContext context, String message) {
    ScaffoldMessenger.of(context)
      ..hideCurrentSnackBar()
      ..showSnackBar(SnackBar(content: Text(message)));
  }

  Future<void> _onSubscribe(
    BuildContext context,
    SubscriptionViewModel vm,
  ) async {
    final tier = _activePlan == _PlanToggle.pro
        ? SubscriptionTier.pro
        : SubscriptionTier.companion;
    final annual = _billingPeriod == _BillingPeriod.annual;

    final opened = await vm.openCheckout(tier: tier, annual: annual);

    if (opened && context.mounted) {
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(
          content: Text(
            'Finishing up in your browser. Buddy unlocks here the moment payment lands.',
          ),
        ),
      );
    }
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

/// Prices as the App Store reports them for this user's storefront, or null
/// when the products have not loaded.
///
/// Apple requires the store's own localized price to be what is displayed, so
/// the hardcoded USD constants above must never reach an iOS screen: a buyer in
/// India would be shown dollars and charged rupees. `symbol` is deliberately
/// empty because [ProductDetails.price] is already a fully formatted string.
_PlanPricing? _storePricingForPlan(
  StorePurchaseService store,
  _PlanToggle plan,
) {
  final tier = plan == _PlanToggle.pro
      ? SubscriptionTier.pro
      : SubscriptionTier.companion;
  final monthly = store.productFor(tier, annual: false);
  final annual = store.productFor(tier, annual: true);
  if (monthly == null || annual == null) return null;
  return _PlanPricing(
    symbol: '',
    monthly: monthly.price,
    annual: annual.price,
    // Derived from the raw amount rather than parsed out of the formatted
    // string, which varies by locale in separator, symbol and position.
    monthlyEquivalent:
        '${annual.currencySymbol}${(annual.rawPrice / 12).toStringAsFixed(2)}',
  );
}

// Free / Companion / Pro toggle

class _PlanToggleSwitch extends StatelessWidget {
  final _PlanToggle selected;

  /// The tier the account pays for, or null when nothing is owned. Rendered
  /// independently of [selected] so ownership stays visible while browsing.
  final _PlanToggle? owned;

  final ValueChanged<_PlanToggle> onChanged;

  const _PlanToggleSwitch({
    required this.selected,
    required this.owned,
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
            isOwned: owned == _PlanToggle.free,
            onTap: () => onChanged(_PlanToggle.free),
          ),
          _ToggleSegment(
            label: 'Companion',
            isSelected: selected == _PlanToggle.companion,
            isOwned: owned == _PlanToggle.companion,
            onTap: () => onChanged(_PlanToggle.companion),
          ),
          _ToggleSegment(
            label: 'Pro',
            isSelected: selected == _PlanToggle.pro,
            isOwned: owned == _PlanToggle.pro,
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

  /// The account pays for this tier. Independent of [isSelected]: the toggle
  /// is a browser, so a Pro subscriber reading the Free column must still see
  /// Pro marked. The segment itself keeps its normal fill; ownership is a
  /// badge pinned over its top-right corner.
  final bool isOwned;

  final VoidCallback onTap;

  const _ToggleSegment({
    required this.label,
    required this.isSelected,
    required this.isOwned,
    required this.onTap,
  });

  @override
  Widget build(BuildContext context) {
    final segment = AnimatedContainer(
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
          fontWeight: FontWeight.w700,
        ),
      ),
    );

    return Expanded(
      child: Semantics(
        selected: isSelected,
        label: isOwned ? '$label, your current plan' : label,
        child: GestureDetector(
          onTap: onTap,
          child: isOwned
              // Clip.none so the badge can straddle the corner and break the
              // toggle's outline, which is what makes it read as applied on
              // top rather than as another segment state. The Stack takes its
              // size from the segment, so the badge costs no layout.
              ? Stack(
                  clipBehavior: Clip.none,
                  children: [
                    segment,
                    const Positioned(top: -11, right: -8, child: _OwnedBadge()),
                  ],
                )
              : segment,
        ),
      ),
    );
  }
}

/// The "YOUR PLAN" tag that sits over the owned tier, shaped like the BETA
/// badge on the Aura-Desktop download button: a small light pill lifted off
/// the surface it marks.
class _OwnedBadge extends StatelessWidget {
  const _OwnedBadge();

  @override
  Widget build(BuildContext context) {
    final badge = Container(
      padding: const EdgeInsets.symmetric(horizontal: 9, vertical: 4),
      decoration: BoxDecoration(
        color: AppColors.premium,
        borderRadius: BorderRadius.circular(30),
        border: Border.all(color: AppColors.surface, width: 1.5),
        boxShadow: [
          BoxShadow(
            color: AppColors.textPrimary.withValues(alpha: 0.16),
            blurRadius: 6,
            offset: const Offset(0, 2),
          ),
        ],
      ),
      child: const Text(
        'YOUR PLAN',
        style: TextStyle(
          // Dark on gold is 6.6:1; white on it would be 2.2:1.
          color: AppColors.textPrimary,
          fontSize: 9,
          fontWeight: FontWeight.w700,
          letterSpacing: 0.6,
          height: 1.1,
        ),
      ),
    );

    // A looping flash is exactly what Reduce Motion exists to suppress, and
    // the badge says everything it needs to without moving.
    if (MediaQuery.disableAnimationsOf(context)) return badge;
    return ShimmerSweep(
      begin: Alignment.bottomLeft,
      end: Alignment.topRight,
      repeat: true,
      duration: const Duration(milliseconds: 2000),
      // Narrower than the default, which is tuned for a ~300px card stack and
      // would light this whole badge at once as a wash rather than a streak.
      bandHalfWidth: 0.16,
      child: badge,
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
    text: '10 voice minutes every day',
    included: true,
  ),
  _FeatureItem(
    icon: Icons.chat_bubble_outline_rounded,
    text: '25 chat messages every day',
    included: true,
  ),
  _FeatureItem(
    icon: Icons.memory_rounded,
    text: 'Aura memory that remembers you',
    included: true,
  ),
  _FeatureItem(
    icon: Icons.calendar_today_outlined,
    text: 'Calendar + reminders',
    included: true,
  ),
  _FeatureItem(
    icon: Icons.mail_outline_rounded,
    text: 'Gmail agent + priority voice',
    included: false,
  ),
];

const _companionFeatureItems = [
  _FeatureItem(
    icon: Icons.record_voice_over_outlined,
    text: 'Unlimited voice, whenever you want',
    included: true,
  ),
  _FeatureItem(
    icon: Icons.all_inclusive_rounded,
    text: 'Unlimited chat',
    included: true,
  ),
  _FeatureItem(
    icon: Icons.calendar_today_outlined,
    text: 'Calendar + reminders',
    included: true,
  ),
  _FeatureItem(
    icon: Icons.memory_rounded,
    text: 'Unlimited Aura memory',
    included: true,
  ),
  _FeatureItem(
    icon: Icons.bolt_outlined,
    text: 'Always-on voice, no waiting',
    included: true,
  ),
];

const _proFeatureItems = [
  _FeatureItem(
    icon: Icons.record_voice_over_outlined,
    text: 'Unlimited voice, whenever you want',
    included: true,
  ),
  _FeatureItem(
    icon: Icons.mail_outline_rounded,
    text: 'Gmail agent + email actions',
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
            // 14 replaces the 10 + 1 + 10 the rules used to occupy. No fixed
            // row height: the Stack sizer around the PageView reads these rows'
            // intrinsic height, and pinning it is what would clip a label that
            // wraps at large text sizes.
            if (i > 0) const SizedBox(height: 14),
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
        // Bare icon, no tile behind it. The fixed width keeps the labels on
        // a common left edge now that there is no box setting the rhythm.
        SizedBox(
          width: 28,
          child: Icon(
            item.icon,
            color: item.included ? AppColors.accent : AppColors.textDisabled,
            size: 18,
          ),
        ),
        const SizedBox(width: 12),
        Expanded(
          child: Text(
            item.text,
            style: TextStyle(
              color: item.included
                  ? AppColors.textSecondary
                  : AppColors.textDisabled,
              fontSize: 16,
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
                  : [const Color(0x0F2B2A26), const Color(0x082B2A26)],
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
                      color: isActive
                          ? AppColors.accent
                          : AppColors.textPrimary,
                      fontSize: 13,
                      fontWeight: FontWeight.w600,
                    ),
                  ),
                  if (isAnnual) ...[
                    const SizedBox(width: 5),
                    Container(
                      padding: const EdgeInsets.symmetric(
                        horizontal: 5,
                        vertical: 2,
                      ),
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
                  style: TextStyle(color: AppColors.textTertiary, fontSize: 11),
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
            borderRadius: BorderRadius.circular(30),
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
                    child: CircularProgressIndicator(
                      color: Colors.white,
                      strokeWidth: 2,
                    ),
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
          borderRadius: BorderRadius.circular(30),
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

/// "Tell me when I can upgrade" — the CTA for users with no purchase path yet.
///
/// Now that web checkout is offered in every country, the only people who see
/// this are users still inside their 45-day trial (checkout answers 409
/// trial_active until it ends) and the case where Dodo is unconfigured. It is a
/// request to be told rather than a purchase, so it mentions no price and links
/// to no checkout. It just lets someone raise their hand early.
class _UpgradeInterestButton extends StatelessWidget {
  final bool registered;
  final VoidCallback onTap;

  const _UpgradeInterestButton({required this.registered, required this.onTap});

  @override
  Widget build(BuildContext context) {
    return GestureDetector(
      onTap: registered ? null : onTap,
      child: Container(
        height: 54,
        decoration: BoxDecoration(
          borderRadius: BorderRadius.circular(30),
          border: Border.all(
            color: registered
                ? AppColors.glassBorderLight
                : AppColors.accent.withValues(alpha: 0.55),
          ),
        ),
        child: Center(
          child: Row(
            mainAxisAlignment: MainAxisAlignment.center,
            children: [
              if (registered) ...[
                const Icon(Icons.check, size: 18, color: AppColors.accent),
                const SizedBox(width: 8),
              ],
              Text(
                registered
                    ? "Got it — I'll let you know"
                    : 'Tell me when I can upgrade',
                style: TextStyle(
                  color: registered
                      ? AppColors.textSecondary
                      : AppColors.accent,
                  fontSize: 16,
                  fontWeight: FontWeight.w600,
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}

// Plan status card, shown instead of any purchase UI when the user cannot buy
// right now (still trialing, or checkout unconfigured) or already pays.
// Status only: current plan, trial countdown, renewal state.

class _PlanStatusCard extends StatelessWidget {
  final SubscriptionViewModel vm;

  const _PlanStatusCard({required this.vm});

  String get _statusLine {
    if (vm.isTrialActive) {
      final days = vm.daysLeftInTrial;
      return days == 1
          ? 'Trial: 1 day left with the full Pro experience.'
          : 'Trial: $days days left with the full Pro experience.';
    }
    if (vm.isPaid) {
      if (vm.entitlement?.cancelAtPeriodEnd == true) {
        return 'Your plan stays active until the end of this billing period.';
      }
      // Naming the website is a purchase-adjacent pointer, and App Store review
      // reads those as steering to outside payment — so on iOS this card is not
      // rendered at all rather than saying something empty in its place. The
      // gold segment in the toggle above is what confirms the plan now.
      return 'Manage your plan anytime at auravoiceapp.com.';
    }
    // Trial over and nothing to buy. Say so plainly and immediately follow it
    // with what they DO still have (the usage rows below). This screen is
    // reached automatically when someone hits their daily cap, so landing on
    // vague copy and no numbers is what made it feel like a dead end.
    return "Your free trial has ended, so you're on the free plan now. "
        "Here's what you've got today.";
  }

  /// The metered allowances, when the backend told us. Only rendered for
  /// free-tier accounts: a paying user has no caps worth showing.
  List<Widget> _usageRows() {
    final usage = vm.usage;
    if (usage == null || vm.isPaid || vm.isTrialActive) return const [];

    final rows = <Widget>[];
    void add(String label, UsageCounter? counter, {bool isDuration = false}) {
      if (counter == null) return;
      rows.add(
        _UsageRow(label: label, counter: counter, isDuration: isDuration),
      );
    }

    add('Messages', usage.chat);
    add('Web searches', usage.webSurf);
    add('Screen drafts', usage.drafts);
    add('Voice', usage.voiceSeconds, isDuration: true);

    if (rows.isEmpty) return const [];
    return [
      const SizedBox(height: 18),
      ...rows,
      const SizedBox(height: 4),
      const Text(
        'Resets at midnight UTC.',
        style: TextStyle(color: AppColors.textTertiary, fontSize: 12),
      ),
    ];
  }

  @override
  Widget build(BuildContext context) {
    return FauxGlassCard(
      borderRadius: 18,
      padding: const EdgeInsets.symmetric(horizontal: 18, vertical: 20),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          // The plan name is deliberately absent: the gold segment in the
          // toggle above is the single place it is stated. This card carries
          // what that cannot — renewal state, trial countdown, allowances.
          Text(
            _statusLine,
            style: const TextStyle(
              color: AppColors.textSecondary,
              fontSize: 14,
              height: 1.4,
            ),
          ),
          ..._usageRows(),
        ],
      ),
    );
  }
}

/// One allowance, as "Messages · 18 of 25" with a proportional bar.
///
/// The bar matters more than the numbers: it is what lets someone see at a
/// glance that they are nearly out, which is the information this app has never
/// given anyone before hitting the wall.
class _UsageRow extends StatelessWidget {
  final String label;
  final UsageCounter counter;
  final bool isDuration;

  const _UsageRow({
    required this.label,
    required this.counter,
    this.isDuration = false,
  });

  /// Voice is metered in seconds, which nobody thinks in. Show minutes.
  String get _valueText {
    if (!isDuration) return '${counter.used} of ${counter.limit}';
    final usedMinutes = (counter.used / 60).floor();
    final limitMinutes = (counter.limit / 60).floor();
    return '$usedMinutes of $limitMinutes min';
  }

  @override
  Widget build(BuildContext context) {
    final isExhausted = counter.isExhausted;
    final barColor = isExhausted
        ? AppColors.error
        : counter.isRunningLow
        ? AppColors.warning
        : AppColors.accent;

    return Padding(
      padding: const EdgeInsets.only(top: 12),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            mainAxisAlignment: MainAxisAlignment.spaceBetween,
            children: [
              Text(
                label,
                style: const TextStyle(
                  color: AppColors.textSecondary,
                  fontSize: 13,
                ),
              ),
              Text(
                _valueText,
                style: TextStyle(
                  color: isExhausted ? AppColors.error : AppColors.textPrimary,
                  fontSize: 13,
                  fontWeight: FontWeight.w600,
                ),
              ),
            ],
          ),
          const SizedBox(height: 6),
          ClipRRect(
            borderRadius: BorderRadius.circular(3),
            child: LinearProgressIndicator(
              value: counter.fraction,
              minHeight: 5,
              backgroundColor: AppColors.textTertiary.withValues(alpha: 0.18),
              valueColor: AlwaysStoppedAnimation<Color>(barColor),
            ),
          ),
        ],
      ),
    );
  }
}
