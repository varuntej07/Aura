import 'dart:async';

import 'package:firebase_auth/firebase_auth.dart';
import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';

import '../../core/config/environment.dart';
import '../../core/logging/app_logger.dart';

/// Mirrors the signed-in session to the Buddy Keyboard.
///
/// The keyboard runs in its own OS process and cannot see the app's in-memory
/// Firebase session, so the app pushes a credential (uid + current Firebase ID token
/// + the active API base URL) across the `dev.varuntej.aura/keyboard` MethodChannel
/// into shared secure storage. The keyboard reads it to authenticate
/// POST /keyboard/draft. Sign-out clears it.
///
/// Listens to [FirebaseAuth.idTokenChanges], which fires on sign-in, sign-out, AND
/// the hourly token refresh, so the keyboard's token stays fresh without polling.
///
/// INTERIM (M0.2 lite): the shared credential is the short-lived Firebase ID token,
/// which the backend already verifies. The production design mints a dedicated,
/// revocable token at POST /keyboard/token; that swap lands here without the keyboard
/// changing.
class KeyboardCredentialBridge {
  KeyboardCredentialBridge._();
  static final KeyboardCredentialBridge instance = KeyboardCredentialBridge._();

  static const MethodChannel _channel = MethodChannel('dev.varuntej.aura/keyboard');

  StreamSubscription<User?>? _sub;
  bool _started = false;

  /// Begins mirroring auth state to the keyboard. Idempotent. Android-only for now;
  /// the iOS keyboard shares its credential through a Keychain access group instead,
  /// wired when that target ships. Safe to call only after Firebase is initialized.
  void start() {
    if (_started) return;
    // TODO(ios): wire a Keychain access-group bridge for the iOS keyboard extension.
    if (defaultTargetPlatform != TargetPlatform.android) return;
    try {
      _sub = FirebaseAuth.instance.idTokenChanges().listen(_onAuthChanged);
      _started = true;
    } catch (e) {
      AppLogger.warning(
        'Keyboard credential bridge failed to start',
        tag: 'KeyboardBridge',
        metadata: {'error': e.toString()},
      );
    }
  }

  Future<void> _onAuthChanged(User? user) async {
    try {
      if (user == null) {
        await _channel.invokeMethod<void>('clearKeyboardCredential');
        return;
      }
      final token = await user.getIdToken();
      if (token == null || token.isEmpty) return;
      await _channel.invokeMethod<void>('setKeyboardCredential', {
        'uid': user.uid,
        'idToken': token,
        'apiBaseUrl': Environment.current.apiBaseUrl,
      });
    } on PlatformException catch (e) {
      // Never let a keyboard-bridge failure affect the app session: it is additive.
      AppLogger.warning(
        'Keyboard credential write failed',
        tag: 'KeyboardBridge',
        metadata: {'error': e.message ?? e.code},
      );
    } catch (e) {
      AppLogger.warning(
        'Keyboard credential bridge error',
        tag: 'KeyboardBridge',
        metadata: {'error': e.toString()},
      );
    }
  }

  void dispose() {
    _sub?.cancel();
    _sub = null;
    _started = false;
  }
}
