import 'package:aura/core/utils/timezone_utils.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  group('canonicalizeTimezoneIdentifier', () {
    test('maps the legacy Kolkata alias to its canonical IANA name', () {
      expect(canonicalizeTimezoneIdentifier('Asia/Calcutta'), 'Asia/Kolkata');
    });

    test('preserves current IANA identifiers and trims whitespace', () {
      expect(canonicalizeTimezoneIdentifier(' America/Los_Angeles '),
          'America/Los_Angeles');
    });

    test('uses UTC for an empty identifier', () {
      expect(canonicalizeTimezoneIdentifier('  '), 'UTC');
    });
  });
}
