import '../../core/base/safe_change_notifier.dart';
import '../../data/models/subscription_plan.dart';
import '../../data/services/subscription_service.dart';

/// Thin ViewModel that exposes [SubscriptionService] state to the UI.
///
/// No business logic lives here; all decisions (caching, checkout session
/// creation) stay in [SubscriptionService]. The VM's job is
/// to drive loading state for the paywall screen and translate user actions
/// into service calls.
class SubscriptionViewModel extends SafeChangeNotifier {
  final SubscriptionService _subscriptionService;

  bool _isOpeningCheckout = false;
  String? _feedbackMessage;

  SubscriptionViewModel({required SubscriptionService subscriptionService})
    : _subscriptionService = subscriptionService {
    _subscriptionService.addListener(_onServiceChanged);
  }

  // Getters

  SubscriptionTier get currentTier => _subscriptionService.currentTier;
  bool get isTrialActive => _subscriptionService.isTrialActive;
  int get daysLeftInTrial => _subscriptionService.daysLeftInTrial;
  bool get hasFeatureAccess => _subscriptionService.hasFeatureAccess;
  bool get isLoading => _subscriptionService.isLoading || _isOpeningCheckout;
  String? get errorMessage => _subscriptionService.errorMessage ?? _feedbackMessage;
  UserEntitlement? get entitlement => _subscriptionService.entitlement;

  /// Convenience getters for the paywall screen, so tier logic is not repeated in UI.
  bool get isOnCompanionPlan => currentTier == SubscriptionTier.companion;
  bool get isOnProPlan => currentTier == SubscriptionTier.pro;
  bool get isOnFreePlan =>
      currentTier == SubscriptionTier.free && !isTrialActive;
  bool get isPaid => _subscriptionService.entitlement?.isPaid ?? false;
  /// Whether to render purchase UI at all. Two conditions, and both must hold:
  ///   * [checkoutAvailable] — the backend can actually create a session.
  ///     Without this the app happily drew an Upgrade button while
  ///     POST /billing/checkout refused every request.
  ///   * the account is past its trial and not already paying.
  ///
  /// Country and storefront are deliberately absent: web checkout is offered
  /// everywhere, and Dodo as merchant of record is what declines a payment it
  /// may not accept.
  bool get canPurchaseSubscription =>
      checkoutAvailable && _subscriptionService.canPurchaseSubscription;

  bool get checkoutAvailable => _subscriptionService.checkoutAvailable;

  /// Today's metered allowances, or null when unknown. Drives the Free-plan
  /// status view and the running-low hint in chat.
  UsageSummary? get usage => _subscriptionService.usage;

  /// True when the account has no purchase path right now — it is still inside
  /// the trial, or the backend cannot create a checkout session — and so must
  /// be shown what it still HAS rather than a dead-end upsell.
  bool get showFreePlanStatus => !canPurchaseSubscription && !isPaid;

  // Actions

  /// Creates the web checkout session and opens the system browser.
  /// The unlock arrives via the entitlement-updated push (or the resume
  /// refetch); nothing is granted client-side.
  Future<bool> openCheckout({
    required SubscriptionTier tier,
    required bool annual,
  }) async {
    _isOpeningCheckout = true;
    _feedbackMessage = null;
    safeNotifyListeners();

    final opened = await _subscriptionService.openCheckout(
      tier: tier,
      annual: annual,
    );

    _isOpeningCheckout = false;
    safeNotifyListeners();
    return opened;
  }

  /// Refetches entitlement from the backend (e.g. when the app resumes after
  /// the user paid in the browser).
  Future<void> refreshEntitlement() =>
      _subscriptionService.refreshEntitlement();

  Future<bool> redeemPromoCode(String code) async {
    _feedbackMessage = null;
    final success = await _subscriptionService.redeemPromoCode(code);
    _feedbackMessage = success ? 'Promo applied!' : 'Invalid or expired code.';
    safeNotifyListeners();
    return success;
  }

  void clearFeedback() {
    _feedbackMessage = null;
    safeNotifyListeners();
  }

  // Private

  void _onServiceChanged() => safeNotifyListeners();

  @override
  void dispose() {
    _subscriptionService.removeListener(_onServiceChanged);
    super.dispose();
  }
}
