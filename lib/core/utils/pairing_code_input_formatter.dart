import 'package:flutter/services.dart';

/// Live-formats desktop pairing-code input to match the XXXX-XXXX code shown
/// on the phone: keeps only letters and digits, uppercases them, and inserts
/// the hyphen after the 4th character. The user types the 8 characters they
/// see, never touching Shift, Caps Lock, or the hyphen key. Pasting the full
/// formatted code works too.
class PairingCodeInputFormatter extends TextInputFormatter {
  const PairingCodeInputFormatter();

  /// Mirrors PAIRING_CODE_LENGTH on the backend (handlers/pairing.py).
  static const int codeLength = 8;

  /// The bare code with display formatting stripped, ready to submit.
  static String rawCode(String text) =>
      text.replaceAll(RegExp(r'[^A-Za-z0-9]'), '').toUpperCase();

  @override
  TextEditingValue formatEditUpdate(
      TextEditingValue oldValue, TextEditingValue newValue) {
    final raw = rawCode(newValue.text);
    final capped = raw.length > codeLength ? raw.substring(0, codeLength) : raw;
    final formatted = capped.length > 4
        ? '${capped.substring(0, 4)}-${capped.substring(4)}'
        : capped;
    // Caret pinned to the end: hyphen insertion shifts positions, and a code
    // field is append-only in practice.
    return TextEditingValue(
      text: formatted,
      selection: TextSelection.collapsed(offset: formatted.length),
    );
  }
}
