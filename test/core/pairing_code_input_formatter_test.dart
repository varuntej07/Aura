import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:aura/core/utils/pairing_code_input_formatter.dart';

TextEditingValue _format(String typed) {
  const formatter = PairingCodeInputFormatter();
  return formatter.formatEditUpdate(
    TextEditingValue.empty,
    TextEditingValue(text: typed),
  );
}

void main() {
  group('PairingCodeInputFormatter', () {
    test('uppercases lowercase typing so Caps Lock is never needed', () {
      expect(_format('abcd').text, 'ABCD');
    });

    test('inserts the hyphen after the 4th character by itself', () {
      expect(_format('abcd2').text, 'ABCD-2');
      expect(_format('abcd2345').text, 'ABCD-2345');
    });

    test('accepts a pasted code already in display form', () {
      expect(_format('ABCD-2345').text, 'ABCD-2345');
    });

    test('accepts a pasted code with spaces and mixed case', () {
      expect(_format(' abcd 2345 ').text, 'ABCD-2345');
    });

    test('caps input at 8 code characters', () {
      expect(_format('abcd2345extra').text, 'ABCD-2345');
    });

    test('drops characters that can never appear in a code', () {
      expect(_format('ab!cd@23#45').text, 'ABCD-2345');
    });

    test('keeps the caret at the end across the hyphen insertion', () {
      final value = _format('abcd2');
      expect(value.selection, const TextSelection.collapsed(offset: 6));
    });

    test('rawCode strips display formatting back to the bare code', () {
      expect(PairingCodeInputFormatter.rawCode('ABCD-2345'), 'ABCD2345');
      expect(PairingCodeInputFormatter.rawCode('abcd 2345'), 'ABCD2345');
      expect(PairingCodeInputFormatter.rawCode('AB'), 'AB');
    });
  });
}
