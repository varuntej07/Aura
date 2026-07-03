import 'dart:async';
import 'dart:convert';
import 'dart:io' show Platform, SocketException;

import 'package:firebase_auth/firebase_auth.dart';
import 'package:http/http.dart' as http;

import '../../../core/config/environment.dart';
import '../../../core/errors/app_exception.dart';
import '../../../core/logging/app_logger.dart';
import '../../../core/network/api_response.dart';

const _tag = 'Pairing';

/// Desktop phone-pairing sign-in (review decision 2): the phone generates a
/// short-lived single-use code, this PC redeems it for a Firebase custom
/// token and signs in with it. The normal auth stream picks the session up
/// from there; no shared auth code changes.
///
/// Uses raw http deliberately: ApiClient throws sessionTokenUnavailable on
/// unauthenticated calls, and the claim is pre-auth by definition (same
/// pattern as VoiceSessionService's token fetch).
class PairingService {
  PairingService({http.Client? httpClient, FirebaseAuth? firebaseAuth})
      : _http = httpClient ?? http.Client(),
        _firebaseAuthOverride = firebaseAuth;

  final http.Client _http;
  final FirebaseAuth? _firebaseAuthOverride;

  FirebaseAuth get _firebaseAuth =>
      _firebaseAuthOverride ?? FirebaseAuth.instance;

  Future<Result<void>> claimAndSignIn(String rawCode) async {
    final code = rawCode.replaceAll(RegExp(r'[\s-]'), '').toUpperCase();
    if (code.length != 8) {
      return Result.failure(AppException.unexpected(
          'That code looks off. It should be 8 letters and numbers.'));
    }

    final String customToken;
    try {
      final response = await _http
          .post(
            Uri.parse('${Environment.current.apiBaseUrl}/devices/pair/claim'),
            headers: {'Content-Type': 'application/json'},
            body: jsonEncode({
              'code': code,
              'device_name': Platform.localHostname,
            }),
          )
          .timeout(const Duration(seconds: 15));

      if (response.statusCode == 400) {
        return Result.failure(AppException.unexpected(
            "That code didn't match or has expired. Grab a fresh one on your phone."));
      }
      if (response.statusCode != 200) {
        AppLogger.error('Pairing claim failed',
            tag: _tag, metadata: {'status': response.statusCode});
        return Result.failure(AppException.unexpected(
            "Couldn't link this PC. Give it another try in a sec?"));
      }
      final body = jsonDecode(response.body) as Map<String, dynamic>;
      final token = body['custom_token'] as String?;
      if (token == null) {
        AppLogger.error('Pairing claim returned no token', tag: _tag);
        return Result.failure(AppException.unexpected(
            "Couldn't link this PC. Give it another try in a sec?"));
      }
      customToken = token;
    } on TimeoutException {
      return Result.failure(AppException.unexpected(
          "Linking timed out. Check your connection and try again."));
    } on SocketException {
      return Result.failure(AppException.unexpected(
          "Couldn't reach Aura. Check your connection and try again."));
    } catch (e) {
      AppLogger.error('Pairing claim failed', error: e, tag: _tag);
      return Result.failure(AppException.unexpected(
          "Couldn't link this PC. Give it another try in a sec?"));
    }

    try {
      await _firebaseAuth.signInWithCustomToken(customToken);
      AppLogger.info('Paired sign-in complete', tag: _tag);
      return const Result.success(null);
    } on FirebaseAuthException catch (e) {
      AppLogger.error('Paired sign-in failed', error: e, tag: _tag);
      // Clock skew makes custom tokens invalid before their time (journey J3).
      return Result.failure(AppException.unexpected(
          "Got the code, but sign-in tripped. Check your PC's date and time are right, then try again."));
    }
  }
}
