import 'dart:async';
import 'dart:convert';

import 'package:flutter/foundation.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:url_launcher/url_launcher.dart';

import '../../core/constants/api_endpoints.dart';
import '../../core/logging/app_logger.dart';
import '../../core/network/api_client.dart';
import '../models/subscription_plan.dart';
import 'firebase_auth_service.dart';
import 'posthog_analytics_service.dart';

const _tag = 'SubscriptionService';

/// Set by main.dart's background FCM handler when an entitlement push arrives
/// while the app is backgrounded (the background isolate cannot reach this
/// isolate's streams, SharedPreferences is the only channel); consumed by
/// [SubscriptionService.consumePendingBackgroundRefresh] on the next resume.
const kEntitlementRefreshPendingKey = 'entitlement_refresh_pending_v1';

/// Reads the account's subscription state from the backend and opens the web
/// checkout. This client never writes entitlement anywhere: the backend stamps
/// the 45-day trial on the first GET /entitlement and the Dodo payment webhook
/// is the doc's only writer. In-app purchases are gone entirely; purchases
/// happen on the web and unlock every device through the shared account.
///
/// Checkout is offered in every country. There is no storefront or country
/// gating left: the only thing that can suppress the purchase path is the
/// backend reporting that it cannot create a session at all
/// ([checkoutAvailable]). Dodo is the merchant of record and is the party that
/// declines a payment it may not legally accept, so this client never
/// geo-blocks anyone.
///
/// Offline behavior: the last good /entitlement response is cached locally and
/// honored for up to 7 days when the fetch fails, then access degrades to free.
/// Never crash, never lock out, never silently grant.
///
/// In dev mode the backend is bypassed and a hardcoded Pro entitlement is
/// returned so UI work needs no backend or account.
class SubscriptionService extends ChangeNotifier {
  final FirebaseAuthService _authService;
  final PostHogAnalyticsService _postHogAnalyticsService;
  final ApiClient _apiClient;

  UserEntitlement? _entitlement;
  bool _isLoading = false;
  String? _errorMessage;

  static const _cacheKey = 'entitlement_cache_v1';
  static const _offlineGrace = Duration(days: 7);

  SubscriptionService({
    required FirebaseAuthService authService,
    required PostHogAnalyticsService postHogAnalyticsService,
    required ApiClient apiClient,
  }) : _authService = authService,
       _postHogAnalyticsService = postHogAnalyticsService,
       _apiClient = apiClient;

  // ── Getters ────────────────────────────────────────────────────────────────

  UserEntitlement? get entitlement => _entitlement;
  bool get isLoading => _isLoading;
  String? get errorMessage => _errorMessage;

  SubscriptionTier get currentTier =>
      _entitlement?.effectiveTier ?? SubscriptionTier.free;

  bool get isTrialActive => _entitlement?.isTrialActive ?? false;
  int get daysLeftInTrial => _entitlement?.daysLeftInTrial ?? 0;

  bool get hasFeatureAccess => _entitlement?.hasFeatureAccess ?? false;

  bool get canPurchaseSubscription =>
      _entitlement?.canPurchaseSubscription ?? false;

  /// Whether the backend can actually create a checkout session. Serving this
  /// from the backend is what lets payments be switched on by configuration
  /// alone, with no app release: the moment Dodo is configured, every installed
  /// client starts showing the purchase UI on its next entitlement fetch.
  bool get checkoutAvailable => _entitlement?.checkoutAvailable ?? false;

  /// Today's metered usage, or null when unknown (cache hit, or offline).
  UsageSummary? get usage => _entitlement?.usage;

  // ── Entitlement fetch ──────────────────────────────────────────────────────

  /// Fetches entitlement from the backend. Call after sign-in and whenever an
  /// entitlement-updated push arrives. Safe to call repeatedly.
  ///
  /// On failure the last cached copy is served if it is fresher than 7 days,
  /// otherwise access degrades to free until a fetch succeeds.
  Future<void> refreshEntitlement() async {
    // Gated on the BUILD MODE, not on Environment.isDev.
    //
    // `ENV` is a --dart-define that no build command in this repo passed, so
    // every shipped Play build resolved to `dev` and took this branch: the app
    // fabricated a Pro entitlement, never called GET /entitlement, and showed a
    // 45-day countdown that reset on every launch — while the backend was
    // enforcing the real trial and capping free users at 25 messages a day.
    // Users hit that cap with the app still calling them Pro.
    //
    // kDebugMode is compiler truth. A release binary can never take this path.
    if (kDebugMode) {
      _entitlement = _devProEntitlement();
      AppLogger.info(
        'Debug build: subscription bypassed with Pro entitlement',
        tag: _tag,
      );
      notifyListeners();
      return;
    }

    final uid = _authService.currentUser?.uid;
    if (uid == null) return;

    _setLoading(true);

    final result = await _apiClient.get<Map<String, dynamic>>(
      ApiEndpoints.entitlement,
      (json) => json,
    );

    await result.when(
      success: (json) async {
        _entitlement = UserEntitlement.fromBackend(json);
        _errorMessage = null;
        await _writeCache(uid, json);
        AppLogger.info(
          'Entitlement loaded',
          tag: _tag,
          metadata: {
            'tier': _entitlement!.tier.name,
            'status': _entitlement!.status.name,
          },
        );
      },
      failure: (error) async {
        AppLogger.warning(
          'Entitlement fetch failed, trying cache',
          tag: _tag,
          metadata: {'error': error.message},
        );
        await _loadFromCache(uid);
      },
    );

    _setLoading(false);
  }

  /// Refetches entitlement if the background FCM isolate flagged that a
  /// billing push arrived while the app was backgrounded. Cheap no-op when
  /// nothing is pending; called from a global on-resume lifecycle hook.
  Future<void> consumePendingBackgroundRefresh() async {
    try {
      final prefs = await SharedPreferences.getInstance();
      // The flag was written by another isolate after this isolate's
      // preference cache loaded; reload() is what makes it visible.
      await prefs.reload();
      if (!(prefs.getBool(kEntitlementRefreshPendingKey) ?? false)) return;
      await prefs.remove(kEntitlementRefreshPendingKey);
      AppLogger.info(
        'Background entitlement push pending, refetching',
        tag: _tag,
      );
    } catch (e) {
      AppLogger.warning(
        'Background refresh marker check failed',
        tag: _tag,
        metadata: {'error': e.toString()},
      );
      return;
    }
    await refreshEntitlement();
  }

  // ── Web checkout ───────────────────────────────────────────────────────────

  /// Creates a checkout session on the backend and opens it in the system
  /// browser (external, never a webview: that is what keeps the link-out
  /// store-compliant). Returns true when the browser was opened.
  Future<bool> openCheckout({
    required SubscriptionTier tier,
    required bool annual,
  }) async {
    if (tier == SubscriptionTier.free || !canPurchaseSubscription) {
      return false;
    }
    _clearError();
    final period = annual ? 'yearly' : 'monthly';

    unawaited(
      _postHogAnalyticsService.trackEvent(
        'checkout_opened',
        properties: {'tier': tier.name, 'billing_period': period},
      ),
    );

    final result = await _apiClient.post<String>(ApiEndpoints.billingCheckout, {
      'tier': tier.name,
      'period': period,
    }, (json) => json['checkout_url'] as String? ?? '');

    final url = result.dataOrNull;
    if (url == null || url.isEmpty) {
      AppLogger.error(
        'Checkout session creation failed',
        tag: _tag,
        metadata: {
          'error': result.errorOrNull?.message ?? 'empty checkout_url',
        },
      );
      _errorMessage =
          "Couldn't reach checkout right now. Give it another try in a bit.";
      notifyListeners();
      return false;
    }

    try {
      final opened = await launchUrl(
        Uri.parse(url),
        mode: LaunchMode.externalApplication,
      );
      if (!opened) {
        _errorMessage = "Couldn't open your browser. Try again from Settings.";
        notifyListeners();
      }
      return opened;
    } catch (e, st) {
      AppLogger.error(
        'Checkout launch failed',
        error: e,
        stackTrace: st,
        tag: _tag,
      );
      _errorMessage = "Couldn't open your browser. Try again from Settings.";
      notifyListeners();
      return false;
    }
  }

  // ── Promo code redemption ──────────────────────────────────────────────────

  /// Redeems a custom promo code via the backend. Not yet wired.
  Future<bool> redeemPromoCode(String code) async {
    AppLogger.info('Promo redemption not yet wired to backend', tag: _tag);
    return false;
  }

  // ── Private: offline cache ─────────────────────────────────────────────────

  Future<void> _writeCache(String uid, Map<String, dynamic> json) async {
    try {
      final prefs = await SharedPreferences.getInstance();
      await prefs.setString(
        _cacheKey,
        jsonEncode({
          'uid': uid,
          'fetched_at': DateTime.now().toIso8601String(),
          'entitlement': UserEntitlement.fromBackend(json).toCacheJson(),
        }),
      );
    } catch (e) {
      AppLogger.warning(
        'Entitlement cache write failed',
        tag: _tag,
        metadata: {'error': e.toString()},
      );
    }
  }

  /// Serves the cached entitlement when it belongs to this uid and is fresher
  /// than the 7-day offline grace window; otherwise degrades to free.
  Future<void> _loadFromCache(String uid) async {
    try {
      final prefs = await SharedPreferences.getInstance();
      final raw = prefs.getString(_cacheKey);
      if (raw != null) {
        final cached = jsonDecode(raw) as Map<String, dynamic>;
        final fetchedAt = DateTime.tryParse(
          cached['fetched_at'] as String? ?? '',
        );
        final cachedUid = cached['uid'] as String?;
        if (cachedUid == uid &&
            fetchedAt != null &&
            DateTime.now().difference(fetchedAt) < _offlineGrace) {
          _entitlement = UserEntitlement.fromCacheJson(
            (cached['entitlement'] as Map).cast<String, dynamic>(),
          );
          AppLogger.info(
            'Serving cached entitlement (offline grace)',
            tag: _tag,
          );
          notifyListeners();
          return;
        }
      }
    } catch (e) {
      AppLogger.warning(
        'Entitlement cache read failed',
        tag: _tag,
        metadata: {'error': e.toString()},
      );
    }

    // No usable cache: degrade to free (never crash, never lock out UI, and
    // never silently grant paid access we cannot confirm).
    _entitlement = null;
    notifyListeners();
  }

  // ── Private: helpers ───────────────────────────────────────────────────────

  UserEntitlement _devProEntitlement() {
    final now = DateTime.now();
    return UserEntitlement(
      tier: SubscriptionTier.pro,
      status: SubscriptionStatus.active,
      serverEffectiveTier: SubscriptionTier.pro,
      trialEndDate: now.add(const Duration(days: kTrialDurationDays)),
    );
  }

  void _setLoading(bool value) {
    _isLoading = value;
    notifyListeners();
  }

  void _clearError() {
    _errorMessage = null;
  }
}
