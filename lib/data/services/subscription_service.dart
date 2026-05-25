import 'dart:async';

import 'package:cloud_firestore/cloud_firestore.dart';
import 'package:flutter/foundation.dart';
import 'package:in_app_purchase/in_app_purchase.dart';

import '../../core/config/environment.dart';
import '../../core/logging/app_logger.dart';
import '../models/subscription_plan.dart';
import 'firebase_auth_service.dart';
import 'firestore_service.dart';

const _tag = 'SubscriptionService';

/// Manages the full subscription lifecycle: store initialisation, product
/// loading, purchase, restore, and Firestore entitlement sync.
///
/// In dev mode the store is bypassed entirely and a hardcoded Pro entitlement
/// is returned so UI work can proceed without StoreKit / Play Billing.
///
/// Entitlement source of truth is Firestore `users/{uid}/entitlement`.
/// The backend entitlement middleware reads the same doc — this is the single
/// field both sides share.
class SubscriptionService extends ChangeNotifier {
  final FirebaseAuthService _authService;

  StreamSubscription<List<PurchaseDetails>>? _purchaseStreamSub;

  UserEntitlement? _entitlement;
  List<ProductDetails> _products = const [];
  bool _isStoreAvailable = false;
  bool _isLoading = false;
  String? _errorMessage;

  SubscriptionService({
    required FirestoreService firestoreService,
    required FirebaseAuthService authService,
  })  : _authService = authService;

  // ── Getters ────────────────────────────────────────────────────────────────

  UserEntitlement? get entitlement => _entitlement;
  List<ProductDetails> get products => _products;
  bool get isStoreAvailable => _isStoreAvailable;
  bool get isLoading => _isLoading;
  String? get errorMessage => _errorMessage;

  SubscriptionTier get currentTier =>
      _entitlement?.effectiveTier ?? SubscriptionTier.free;

  bool get isTrialActive => _entitlement?.isTrialActive ?? false;
  int get daysLeftInTrial => _entitlement?.daysLeftInTrial ?? 0;

  bool get hasFeatureAccess => _entitlement?.hasFeatureAccess ?? false;

  // ── Init ───────────────────────────────────────────────────────────────────

  /// Call once after the user is authenticated. Safe to call multiple times.
  Future<void> initStore() async {
    if (Environment.isDev) {
      _entitlement = _devProEntitlement();
      _isStoreAvailable = false;
      AppLogger.info('Dev mode: subscription bypassed with Pro entitlement', tag: _tag);
      notifyListeners();
      return;
    }

    _setLoading(true);

    await _loadOrCreateEntitlement();

    final available = await InAppPurchase.instance.isAvailable();
    _isStoreAvailable = available;

    if (!available) {
      AppLogger.warning('Store not available on this device', tag: _tag);
      _setLoading(false);
      return;
    }

    _purchaseStreamSub?.cancel();
    _purchaseStreamSub = InAppPurchase.instance.purchaseStream
        .listen(_onPurchaseUpdate, onError: _onPurchaseStreamError);

    await fetchProducts();
    _setLoading(false);
  }

  // ── Products ───────────────────────────────────────────────────────────────

  Future<void> fetchProducts() async {
    if (!_isStoreAvailable) return;
    try {
      final response = await InAppPurchase.instance
          .queryProductDetails(SubscriptionProductIds.all);

      if (response.error != null) {
        AppLogger.error(
          'Product query error',
          tag: _tag,
          metadata: {'error': response.error!.message},
        );
      }

      if (response.notFoundIDs.isNotEmpty) {
        AppLogger.warning(
          'Products not found in store',
          tag: _tag,
          metadata: {'ids': response.notFoundIDs.join(', ')},
        );
      }

      _products = response.productDetails;
      notifyListeners();
    } catch (e, st) {
      AppLogger.error('Failed to fetch products', error: e, stackTrace: st, tag: _tag);
    }
  }

  // ── Purchase ───────────────────────────────────────────────────────────────

  Future<void> purchaseProduct(ProductDetails product) async {
    if (!_isStoreAvailable) return;
    _clearError();

    final uid = _authService.currentUser?.uid;
    final params = PurchaseParam(
      productDetails: product,
      applicationUserName: uid,
    );

    try {
      await InAppPurchase.instance.buyNonConsumable(purchaseParam: params);
    } catch (e, st) {
      AppLogger.error('Purchase initiation failed', error: e, stackTrace: st, tag: _tag);
      _errorMessage = "Purchase couldn't be completed. Try again or contact support.";
      notifyListeners();
    }
  }

  Future<void> restorePurchases() async {
    if (!_isStoreAvailable) return;
    _setLoading(true);
    _clearError();
    try {
      await InAppPurchase.instance.restorePurchases();
    } catch (e, st) {
      AppLogger.error('Restore failed', error: e, stackTrace: st, tag: _tag);
      _errorMessage = "Purchase couldn't be completed. Try again or contact support.";
      _setLoading(false);
      notifyListeners();
    }
  }

  // ── Promo code redemption ──────────────────────────────────────────────────

  /// Redeems a custom promo code via the backend. The backend validates the
  /// code, increments usage, and writes the entitlement to Firestore directly.
  /// This method just refreshes the local entitlement after the backend confirms.
  Future<bool> redeemPromoCode(String code) async {
    // TODO: call POST /redeem-promo once the backend endpoint exists (May 22).
    // For now, signal not implemented.
    AppLogger.info('Promo redemption not yet wired to backend', tag: _tag);
    return false;
  }

  // ── Private: purchase stream ───────────────────────────────────────────────

  Future<void> _onPurchaseUpdate(List<PurchaseDetails> purchases) async {
    for (final purchase in purchases) {
      switch (purchase.status) {
        case PurchaseStatus.pending:
          AppLogger.info('Purchase pending', tag: _tag,
              metadata: {'productId': purchase.productID});

        case PurchaseStatus.purchased:
        case PurchaseStatus.restored:
          await _verifyAndGrantEntitlement(purchase);
          await InAppPurchase.instance.completePurchase(purchase);

        case PurchaseStatus.error:
          AppLogger.error('Purchase error', tag: _tag, metadata: {
            'productId': purchase.productID,
            'error': purchase.error?.message ?? 'unknown',
          });
          _errorMessage = "Purchase couldn't be completed. Try again or contact support.";
          await InAppPurchase.instance.completePurchase(purchase);
          notifyListeners();

        case PurchaseStatus.canceled:
          AppLogger.info('Purchase cancelled', tag: _tag,
              metadata: {'productId': purchase.productID});
      }
    }
    _setLoading(false);
  }

  void _onPurchaseStreamError(Object error, StackTrace st) {
    AppLogger.error('Purchase stream error', error: error, stackTrace: st, tag: _tag);
  }

  /// Optimistic verification: trust the platform receipt and write to Firestore.
  /// Replace with server-side Apple/Google receipt verification post-launch.
  Future<void> _verifyAndGrantEntitlement(PurchaseDetails purchase) async {
    final uid = _authService.currentUser?.uid;
    if (uid == null) {
      AppLogger.warning('Cannot grant entitlement — no user', tag: _tag);
      return;
    }

    final tier = SubscriptionProductIds.tierForProductId(purchase.productID);
    final platform = defaultTargetPlatform == TargetPlatform.iOS
        ? EntitlementPlatform.ios
        : EntitlementPlatform.android;

    final existing = _entitlement;
    final updated = (existing ?? UserEntitlement.newUser()).copyWith(
      tier: tier,
      status: SubscriptionStatus.active,
      platform: platform,
      productId: purchase.productID,
      expiresAt: _expiresAtFromPurchase(purchase),
      originalPurchaseDate: existing?.originalPurchaseDate ?? DateTime.now(),
      updatedAt: DateTime.now(),
    );

    await _writeEntitlementToFirestore(uid, updated);
    _entitlement = updated;
    notifyListeners();

    AppLogger.info('Entitlement granted', tag: _tag,
        metadata: {'tier': tier.name, 'productId': purchase.productID});
  }

  // ── Private: Firestore ─────────────────────────────────────────────────────

  Future<void> _loadOrCreateEntitlement() async {
    final uid = _authService.currentUser?.uid;
    if (uid == null) return;

    try {
      final firestore = FirebaseFirestore.instance;
      final doc = await firestore
          .collection('users')
          .doc(uid)
          .collection('entitlement')
          .doc('current')
          .get();

      if (doc.exists && doc.data() != null) {
        _entitlement = UserEntitlement.fromFirestore(doc.data()!);
        AppLogger.info('Entitlement loaded', tag: _tag,
            metadata: {'tier': _entitlement!.tier.name});
      } else {
        // First install — create trial entitlement
        final newEntitlement = UserEntitlement.newUser();
        await _writeEntitlementToFirestore(uid, newEntitlement);
        _entitlement = newEntitlement;
        AppLogger.info('Trial entitlement created for new user', tag: _tag);
      }
      notifyListeners();
    } catch (e, st) {
      AppLogger.error('Failed to load entitlement', error: e, stackTrace: st, tag: _tag);
    }
  }

  Future<void> _writeEntitlementToFirestore(
    String uid,
    UserEntitlement entitlement,
  ) async {
    try {
      await FirebaseFirestore.instance
          .collection('users')
          .doc(uid)
          .collection('entitlement')
          .doc('current')
          .set(entitlement.toFirestore(), SetOptions(merge: true));
    } catch (e, st) {
      AppLogger.error('Failed to write entitlement', error: e, stackTrace: st, tag: _tag);
    }
  }

  // ── Private: helpers ───────────────────────────────────────────────────────

  /// Extracts an expiry from the purchase. StoreKit and Play Billing surface
  /// this differently — for now we approximate from the product ID.
  /// Replace with receipt parsing once server-side verification is live.
  DateTime? _expiresAtFromPurchase(PurchaseDetails purchase) {
    final id = purchase.productID;
    if (id.contains('annual')) {
      return DateTime.now().add(const Duration(days: 365));
    }
    return DateTime.now().add(const Duration(days: 31));
  }

  UserEntitlement _devProEntitlement() {
    final now = DateTime.now();
    return UserEntitlement(
      tier: SubscriptionTier.pro,
      status: SubscriptionStatus.active,
      platform: null,
      trialStartDate: now,
      trialEndDate: now.add(const Duration(days: 14)),
      updatedAt: now,
    );
  }

  void _setLoading(bool value) {
    _isLoading = value;
    notifyListeners();
  }

  void _clearError() {
    _errorMessage = null;
  }

  // ── Dispose ────────────────────────────────────────────────────────────────

  @override
  void dispose() {
    _purchaseStreamSub?.cancel();
    super.dispose();
  }
}
