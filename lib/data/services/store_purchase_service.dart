import 'dart:async';

import 'package:flutter/foundation.dart';
import 'package:in_app_purchase/in_app_purchase.dart';

import '../../core/constants/api_endpoints.dart';
import '../../core/logging/app_logger.dart';
import '../../core/network/api_client.dart';
import '../models/subscription_plan.dart';

/// StoreKit purchases: the iOS seller.
///
/// iOS cannot offer the Dodo web checkout at all. App Store Guideline 3.1.1
/// forbids sending users to an outside payment page for digital content, so on
/// iPhone this class is the only way to buy, and [SubscriptionService] refuses
/// to open web checkout there.
///
/// The device is never the authority on what someone paid for. A purchase here
/// ends with the signed transaction posted to the backend, which verifies
/// Apple's signature and writes the entitlement; the app then refetches it. The
/// local StoreKit result only decides what to show while that round trip
/// happens.
class StorePurchaseService extends ChangeNotifier {
  final ApiClient _apiClient;
  final InAppPurchase _iap;

  /// Called after the backend has accepted a verified transaction, so the app
  /// can refetch the entitlement that purchase just changed.
  final Future<void> Function() _onEntitlementChanged;

  StreamSubscription<List<PurchaseDetails>>? _subscription;

  List<ProductDetails> _products = const [];
  bool _isAvailable = false;
  bool _isLoading = false;
  bool _isPurchasing = false;
  String? _errorMessage;

  static const _tag = 'StorePurchaseService';

  StorePurchaseService({
    required ApiClient apiClient,
    required Future<void> Function() onEntitlementChanged,
    InAppPurchase? iap,
  }) : _apiClient = apiClient,
       _onEntitlementChanged = onEntitlementChanged,
       _iap = iap ?? InAppPurchase.instance;

  // ── Getters ────────────────────────────────────────────────────────────────

  /// Every product the store returned, cheapest tier first. Empty until
  /// [initialize] has completed, and empty on every non-iOS surface.
  List<ProductDetails> get products => _products;

  /// Whether StoreKit is usable on this device. False on other platforms, and
  /// false on iOS when the store is unreachable or purchases are restricted by
  /// parental controls.
  bool get isAvailable => _isAvailable;
  bool get isLoading => _isLoading;
  bool get isPurchasing => _isPurchasing;
  String? get errorMessage => _errorMessage;

  /// True only where StoreKit is the seller. Keeps the platform test in one
  /// place rather than scattering `Platform.isIOS` through the UI.
  static bool get isStorePlatform =>
      !kIsWeb && defaultTargetPlatform == TargetPlatform.iOS;

  ProductDetails? productFor(SubscriptionTier tier, {required bool annual}) {
    final id = productIdFor(tier, annual: annual);
    if (id == null) return null;
    for (final p in _products) {
      if (p.id == id) return p;
    }
    return null;
  }

  /// The App Store product ids. These must match the products created in App
  /// Store Connect exactly, and match the backend's APPLE_PRODUCT_* settings.
  /// Apple treats a product id as immutable once created, so these are constants
  /// rather than remote config.
  static String? productIdFor(SubscriptionTier tier, {required bool annual}) =>
      switch (tier) {
        SubscriptionTier.companion => annual
            ? 'com.varundevs.aura.companion.yearly'
            : 'com.varundevs.aura.companion.monthly',
        SubscriptionTier.pro => annual
            ? 'com.varundevs.aura.pro.yearly'
            : 'com.varundevs.aura.pro.monthly',
        SubscriptionTier.free => null,
      };

  static const Set<String> _allProductIds = {
    'com.varundevs.aura.companion.monthly',
    'com.varundevs.aura.companion.yearly',
    'com.varundevs.aura.pro.monthly',
    'com.varundevs.aura.pro.yearly',
  };

  // ── Lifecycle ──────────────────────────────────────────────────────────────

  /// Starts listening for purchase updates and loads product metadata.
  ///
  /// The stream listener is attached BEFORE products load, because StoreKit
  /// replays transactions that were interrupted last run - a purchase that
  /// completed while the app was being killed arrives here on next launch, and
  /// dropping it would take money without granting access.
  Future<void> initialize() async {
    if (!isStorePlatform) return;

    _subscription ??= _iap.purchaseStream.listen(
      _onPurchaseUpdates,
      onError: (Object e) {
        AppLogger.error('Purchase stream error', tag: _tag, error: e);
        _errorMessage = 'Something went wrong with the App Store. Try again.';
        notifyListeners();
      },
    );

    _isLoading = true;
    notifyListeners();
    try {
      _isAvailable = await _iap.isAvailable();
      if (_isAvailable) await _loadProducts();
    } catch (e) {
      AppLogger.error('Store init failed', tag: _tag, error: e);
      _isAvailable = false;
    } finally {
      _isLoading = false;
      notifyListeners();
    }
  }

  Future<void> _loadProducts() async {
    final response = await _iap.queryProductDetails(_allProductIds);
    if (response.error != null) {
      AppLogger.error(
        'queryProductDetails failed',
        tag: _tag,
        metadata: {'error': response.error!.message},
      );
    }
    if (response.notFoundIDs.isNotEmpty) {
      // Every id here is a product missing from App Store Connect, or one whose
      // agreement is not yet active. Loud, because the paywall silently loses a
      // plan when this happens.
      AppLogger.error(
        'Store products not found',
        tag: _tag,
        metadata: {'missing': response.notFoundIDs.join(', ')},
      );
    }
    _products = response.productDetails;
  }

  @override
  void dispose() {
    _subscription?.cancel();
    _subscription = null;
    super.dispose();
  }

  // ── Buying ─────────────────────────────────────────────────────────────────

  /// Starts a purchase. Returns once StoreKit has taken over; the result
  /// arrives asynchronously on the purchase stream.
  Future<bool> buy(ProductDetails product) async {
    if (!isStorePlatform || !_isAvailable) return false;
    _errorMessage = null;
    _isPurchasing = true;
    notifyListeners();
    try {
      // Auto-renewing subscriptions go through the non-consumable path.
      return await _iap.buyNonConsumable(
        purchaseParam: PurchaseParam(productDetails: product),
      );
    } catch (e) {
      AppLogger.error('buyNonConsumable failed', tag: _tag, error: e);
      _isPurchasing = false;
      _errorMessage = "That didn't go through. Nothing was charged.";
      notifyListeners();
      return false;
    }
  }

  /// Restore Purchases. Apple requires this on any screen that sells a
  /// subscription, and it is how someone who reinstalled or switched device
  /// gets their plan back.
  Future<void> restore() async {
    if (!isStorePlatform || !_isAvailable) return;
    _errorMessage = null;
    _isPurchasing = true;
    notifyListeners();
    try {
      await _iap.restorePurchases();
    } catch (e) {
      AppLogger.error('restorePurchases failed', tag: _tag, error: e);
      _errorMessage = "Couldn't reach the App Store. Try again in a moment.";
    } finally {
      _isPurchasing = false;
      notifyListeners();
    }
  }

  // ── Purchase stream ────────────────────────────────────────────────────────

  Future<void> _onPurchaseUpdates(List<PurchaseDetails> purchases) async {
    for (final purchase in purchases) {
      switch (purchase.status) {
        case PurchaseStatus.pending:
          _isPurchasing = true;
          notifyListeners();

        case PurchaseStatus.canceled:
          _isPurchasing = false;
          _errorMessage = null; // Backing out is not an error worth surfacing.
          notifyListeners();

        case PurchaseStatus.error:
          _isPurchasing = false;
          AppLogger.error(
            'Purchase failed',
            tag: _tag,
            metadata: {'error': purchase.error?.message ?? 'unknown'},
          );
          _errorMessage = "That didn't go through. Nothing was charged.";
          notifyListeners();

        case PurchaseStatus.purchased:
        case PurchaseStatus.restored:
          await _verifyWithBackend(purchase);
      }

      // A transaction left unfinished is replayed by StoreKit forever and blocks
      // further purchases of the same product, so it must be completed whatever
      // the outcome above.
      if (purchase.pendingCompletePurchase) {
        try {
          await _iap.completePurchase(purchase);
        } catch (e) {
          AppLogger.error('completePurchase failed', tag: _tag, error: e);
        }
      }
    }
  }

  /// Posts the signed transaction to the backend, which is the only thing that
  /// can actually grant the entitlement.
  Future<void> _verifyWithBackend(PurchaseDetails purchase) async {
    final signed = purchase.verificationData.serverVerificationData;
    if (signed.isEmpty) {
      // StoreKit 2 always carries the transaction JWS here. Empty means the
      // plugin fell back to StoreKit 1, which the backend cannot verify.
      AppLogger.error('Purchase carried no verification data', tag: _tag);
      _isPurchasing = false;
      _errorMessage = "We couldn't confirm that purchase. Contact support.";
      notifyListeners();
      return;
    }

    final result = await _apiClient.post<String>(
      ApiEndpoints.appleTransaction,
      {'signed_transaction': signed},
      (json) => json['status'] as String? ?? '',
    );

    if (result.dataOrNull != null) {
      await _onEntitlementChanged();
      _errorMessage = null;
    } else {
      // The purchase is real and Apple keeps telling the backend about it
      // through server notifications, so this is a delay, not a loss. Never
      // word it as a failure: the money has already moved.
      AppLogger.error(
        'Backend purchase verification failed',
        tag: _tag,
        metadata: {'error': result.errorOrNull?.message ?? 'unknown'},
      );
      _errorMessage =
          "Payment went through. Unlocking is taking a moment, reopen the app if it doesn't.";
    }
    _isPurchasing = false;
    notifyListeners();
  }
}
