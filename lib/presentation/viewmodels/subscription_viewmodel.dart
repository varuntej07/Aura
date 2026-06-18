import 'package:in_app_purchase/in_app_purchase.dart';

import '../../core/base/safe_change_notifier.dart';
import '../../core/logging/app_logger.dart';
import '../../data/models/subscription_plan.dart';
import '../../data/services/subscription_service.dart';

/// Thin ViewModel that exposes [SubscriptionService] state to the UI.
///
/// No business logic lives here — all decisions (trial logic, platform
/// routing, Firestore writes) stay in [SubscriptionService].
/// The VM's job is to drive loading state for the paywall screen and
/// translate user actions into service calls.
class SubscriptionViewModel extends SafeChangeNotifier {
  final SubscriptionService _subscriptionService;

  bool _isPurchasing = false;
  bool _isRestoring = false;
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
  bool get isStoreAvailable => _subscriptionService.isStoreAvailable;
  bool get isLoading => _subscriptionService.isLoading || _isPurchasing || _isRestoring;
  List<ProductDetails> get products => _subscriptionService.products;
  String? get errorMessage => _subscriptionService.errorMessage ?? _feedbackMessage;
  UserEntitlement? get entitlement => _subscriptionService.entitlement;

  /// Convenience getters for the paywall screen — avoid repeating tier logic in UI.
  bool get isOnCompanionPlan => currentTier == SubscriptionTier.companion;
  bool get isOnProPlan => currentTier == SubscriptionTier.pro;
  bool get isOnFreePlan => currentTier == SubscriptionTier.free && !isTrialActive;

  // Actions

  /// Beta-only: record interest in a tier instead of opening the store sheet.
  /// Use this everywhere the paywall CTA is tapped until real IAP is enabled.
  Future<void> captureInterest({
    required SubscriptionTier tier,
    required bool annual,
  }) =>
      _subscriptionService.captureInterest(tier: tier, annual: annual);

  Future<void> purchaseCompanion({bool annual = false}) async {
    final productId = annual
        ? SubscriptionProductIds.companionAnnual
        : SubscriptionProductIds.companionMonthly;
    await _purchaseById(productId);
  }

  Future<void> purchasePro({bool annual = false}) async {
    final productId = annual
        ? SubscriptionProductIds.proAnnual
        : SubscriptionProductIds.proMonthly;
    await _purchaseById(productId);
  }

  Future<void> restorePurchases() async {
    _isRestoring = true;
    _feedbackMessage = null;
    safeNotifyListeners();

    await _subscriptionService.restorePurchases();

    _isRestoring = false;
    _feedbackMessage = 'Purchases restored.';
    safeNotifyListeners();
  }

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

  Future<void> _purchaseById(String productId) async {
    final product = products.where((p) => p.id == productId).firstOrNull;

    if (product == null) {
      AppLogger.warning(
        'Product not found, store may not be initialised',
        tag: 'SubscriptionViewModel',
        metadata: {'productId': productId},
      );
      _feedbackMessage = "Purchase couldn't be completed. Try again or contact support.";
      safeNotifyListeners();
      return;
    }

    _isPurchasing = true;
    _feedbackMessage = null;
    safeNotifyListeners();

    await _subscriptionService.purchaseProduct(product);

    _isPurchasing = false;
    safeNotifyListeners();
  }

  void _onServiceChanged() => safeNotifyListeners();

  @override
  void dispose() {
    _subscriptionService.removeListener(_onServiceChanged);
    super.dispose();
  }
}
