import 'dart:async';

import 'package:cloud_firestore/cloud_firestore.dart';

import '../../core/base/safe_change_notifier.dart';
import '../../core/logging/app_logger.dart';
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
  String? _error;
  List<LinkedDevice> _linkedDevices = const [];
  bool _unlinking = false;
  Timer? _countdownTicker;

  bool get generating => _generating;
  String? get error => _error;
  bool get unlinking => _unlinking;
  List<LinkedDevice> get linkedDevices => _linkedDevices;

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
    safeNotifyListeners();

    final result = await _backendApiService.startDevicePairing();
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

  Future<void> unlinkDevice(String deviceId) async {
    _unlinking = true;
    _error = null;
    safeNotifyListeners();
    final result = await _backendApiService.unlinkDevice(deviceId);
    result.when(
      success: (_) {},
      failure: (error) => _error = error.message,
    );
    _unlinking = false;
    await loadLinkedDevices();
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
