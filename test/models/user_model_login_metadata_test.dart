import 'package:flutter_test/flutter_test.dart';

import 'package:aura/data/models/user_model.dart';

/// Login/session metadata is written by AuthRepository keyed on the
/// `UserModel.field*` constants and read back by `UserModel.fromJson` using the
/// same constants. This test pins that writer→reader contract: if a field name
/// drifts on one side, parsing breaks here instead of silently returning a
/// default (the failure mode behind the last_seen/registered_at outage).
void main() {
  Map<String, dynamic> baseDoc() => {
        'uid': 'uid-1',
        'display_name': 'Test User',
        'email': 'test@example.com',
        'created_at': DateTime.utc(2026, 1, 1).toIso8601String(),
        'last_active_at': DateTime.utc(2026, 6, 11).toIso8601String(),
      };

  group('UserModel login metadata', () {
    test('reads every field a login/logout write produces', () {
      // Mirrors exactly what AuthRepository._loginMetadataFields and
      // _recordLogoutMetadata persist (counters land as ints post-increment).
      final doc = baseDoc()
        ..addAll({
          UserModel.fieldLastLoginAt: DateTime.utc(2026, 6, 11, 9, 30).toIso8601String(),
          UserModel.fieldLoginCount: 7,
          UserModel.fieldLastLogoutAt: DateTime.utc(2026, 6, 10, 22, 15).toIso8601String(),
          UserModel.fieldLogoutCount: 6,
          UserModel.fieldIsActive: true,
          UserModel.fieldSignInMethod: 'google',
          UserModel.fieldPlatform: 'android',
        });

      final user = UserModel.fromJson(doc);

      expect(user.lastLoginAt, DateTime.utc(2026, 6, 11, 9, 30));
      expect(user.loginCount, 7);
      expect(user.lastLogoutAt, DateTime.utc(2026, 6, 10, 22, 15));
      expect(user.logoutCount, 6);
      expect(user.isActive, isTrue);
      expect(user.signInMethod, 'google');
      expect(user.platform, 'android');
    });

    test('tolerates docs from older clients that lack the metadata', () {
      final user = UserModel.fromJson(baseDoc());

      expect(user.lastLoginAt, isNull);
      expect(user.loginCount, 0);
      expect(user.lastLogoutAt, isNull);
      expect(user.logoutCount, 0);
      expect(user.isActive, isFalse);
      expect(user.signInMethod, isNull);
      expect(user.platform, isNull);
    });
  });
}
