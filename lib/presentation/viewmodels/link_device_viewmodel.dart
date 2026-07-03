import 'dart:async';

import 'package:cloud_firestore/cloud_firestore.dart';

import '../../core/base/safe_change_notifier.dart';
import '../../core/constants/app_constants.dart';
import '../../core/errors/app_exception.dart';
import '../../core/logging/app_logger.dart';
import '../../core/network/api_response.dart';
import '../../data/services/backend_api_service.dart';

const _tag = 'LinkDeviceVM';

class LinkedDevice {
  final String id;
  final String deviceName;
  final DateTime? linkedAt;

  const LinkedDevice({
    required this.id,
    required this.deviceName,
    this.linkedAt,
  });
}

/// Phone-side state for pairing a desktop: mints the short-lived code the PC
/// redeems, and manages the linked-devices list (with the honest all-session
/// revoke on unlink, review decision).
class LinkDeviceViewModel extends SafeChangeNotifier {
  LinkDeviceViewModel({
    required BackendApiService backendApiService,
    required String? userId,
    FirebaseFirestore? firestore,
  })  : _backendApiService = backendApiService,
        _userId = userId,
        _firestore = firestore ?? FirebaseFirestore.instance;

  final BackendApiService _backendApiService;
  final String? _userId;
  final FirebaseFirestore _firestore;

  String? _code;
  DateTime? _expiresAt;
  bool _generating = false;
  bool _showColdStartHint = false;
  String? _error;
  List<LinkedDevice> _linkedDevices = const [];
  final Set<String> _pendingUnlinkIds = {};
  Timer? _countdownTicker;

  bool get generating => _generating;

  /// True once [generateCode] or [unlinkDevice] has been in flight for a few
  /// seconds — both hit a Cloud Run instance that scales to zero, so a cold
  /// start can plausibly take longer than a warm request without anything
  /// having gone wrong.
  bool get showColdStartHint => _showColdStartHint;
  String? get error => _error;
  List<LinkedDevice> get linkedDevices => _linkedDevices;

  /// Per-device in-flight state (unlinking one device must not disable the
  /// unlink action on the others).
  bool isUnlinking(String deviceId) => _pendingUnlinkIds.contains(deviceId);

  /// XXXX-XXXX display form, null when no active code.
  String? get formattedCode {
    final code = _code;
    if (code == null || code.length != 8 || isExpired) return null;
    return '${code.substring(0, 4)}-${code.substring(4)}';
  }

  bool get isExpired =>
      _expiresAt == null || DateTime.now().isAfter(_expiresAt!);

  Duration get remaining => isExpired
      ? Duration.zero
      : _expiresAt!.difference(DateTime.now());

  Future<void> generateCode() async {
    _generating = true;
    _error = null;
    _showColdStartHint = false;
    safeNotifyListeners();

    final hintTimer = Timer(const Duration(seconds: 4), () {
      _showColdStartHint = true;
      safeNotifyListeners();
    });

    // A cold instance can blow through the client timeout on its first request;
    // retrying is safe (an extra code is harmless — capped server-side at 3
    // live codes per user) and almost always lands on the now-warm instance.
    final result = await _withTimeoutRetry(_backendApiService.startDevicePairing);

    hintTimer.cancel();
    _showColdStartHint = false;

    result.when(
      success: (json) {
        _code = json['code'] as String?;
        final expiresIn = (json['expires_in_seconds'] as num?)?.toInt() ?? 300;
        _expiresAt = DateTime.now().add(Duration(seconds: expiresIn));
        _startCountdown();
      },
      failure: (error) => _error = error.message,
    );
    _generating = false;
    safeNotifyListeners();
  }

  Future<void> loadLinkedDevices() async {
    final userId = _userId;
    if (userId == null) return;
    try {
      final snapshot = await _firestore
          .collection('users')
          .doc(userId)
          .collection('linked_devices')
          .get();
      _linkedDevices = [
        for (final doc in snapshot.docs)
          LinkedDevice(
            id: doc.id,
            deviceName: (doc.data()['device_name'] as String?) ?? 'Windows PC',
            linkedAt: (doc.data()['linked_at'] as Timestamp?)?.toDate(),
          ),
      ];
      safeNotifyListeners();
    } catch (e) {
      AppLogger.error('Failed to load linked devices', error: e, tag: _tag);
    }
  }

  /// Unlinks [deviceId]. Returns true once the removal is confirmed (either
  /// this call or an earlier one that later completed server-side), in which
  /// case the caller should know the account was just signed out everywhere,
  /// including this phone (the honest reviewed unlink semantic).
  Future<bool> unlinkDevice(String deviceId) async {
    _pendingUnlinkIds.add(deviceId);
    _error = null;
    safeNotifyListeners();

    // Unlink is idempotent: repeating it on an already-removed device answers
    // notFound rather than erroring, so retrying an ambiguous timeout (cold
    // instance, or a prior attempt that actually landed after we stopped
    // waiting) is safe and resolves faster than passively polling and hoping.
    final result = await _withTimeoutRetry(
      () => _backendApiService.unlinkDevice(deviceId),
    );
    final confirmed =
        result.isSuccess || result.errorOrNull?.code == ErrorCode.notFound;

    if (!confirmed) {
      _error = result.errorOrNull?.code == ErrorCode.requestTimeout
          ? "Still couldn't confirm the unlink. Check your connection and try again."
          : result.errorOrNull?.message;
    }

    _pendingUnlinkIds.remove(deviceId);
    await loadLinkedDevices();
    safeNotifyListeners();
    return confirmed;
  }

  /// Retries [call] while it fails with a request timeout, up to
  /// [AppConstants.maxApiRetries] attempts total, with exponential backoff.
  /// Only call sites whose repeat is idempotent may use this (documented at
  /// each call site above) — a bare timeout means "we don't know if it worked
  /// yet", not "it failed".
  Future<Result<T>> _withTimeoutRetry<T>(
    Future<Result<T>> Function() call,
  ) async {
    for (var attempt = 0; ; attempt++) {
      final result = await call();
      final isLastAttempt = attempt == AppConstants.maxApiRetries - 1;
      if (result.isSuccess ||
          result.errorOrNull?.code != ErrorCode.requestTimeout ||
          isLastAttempt) {
        return result;
      }
      await Future<void>.delayed(AppConstants.retryBaseDelay * (1 << attempt));
    }
  }

  void _startCountdown() {
    _countdownTicker?.cancel();
    _countdownTicker = Timer.periodic(const Duration(seconds: 1), (_) {
      safeNotifyListeners();
      if (isExpired) _countdownTicker?.cancel();
    });
  }

  @override
  void dispose() {
    _countdownTicker?.cancel();
    super.dispose();
  }
}
