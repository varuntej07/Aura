import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'package:http/http.dart' as http;
import '../constants/app_constants.dart';
import '../config/environment.dart';
import '../errors/app_exception.dart';
import '../errors/network_exception.dart';
import '../logging/app_logger.dart';
import '../logging/latency_tracker.dart';
import 'api_response.dart';
import 'connectivity_service.dart';

class ApiClient {
  final ConnectivityService _connectivity;
  final Future<String?> Function({bool forceRefresh}) _tokenProvider;

  ApiClient({
    required ConnectivityService connectivity,
    required Future<String?> Function({bool forceRefresh}) tokenProvider,
  })  : _connectivity = connectivity,
        _tokenProvider = tokenProvider;

  Future<Map<String, String>> _headers() async {
    final token = await _resolveAuthToken();
    return {
      'Content-Type': 'application/json',
      'Accept': 'application/json',
      if (token != null) 'Authorization': 'Bearer $token',
    };
  }

  /// Fetches the Firebase ID token, retrying once with a forced refresh when the
  /// cached token comes back null. The cache returns null when the token has
  /// expired and the app has been idle (e.g. a cold launch from a notification
  /// tap on a flaky network); the forced refresh fetches a fresh one. 
  /// Returns null only when no token can be obtained at all.
  Future<String?> _resolveAuthToken() async {
    final cached = await _tokenProvider();
    if (cached != null) return cached;
    return _tokenProvider(forceRefresh: true);
  }

  Future<Result<T>> get<T>(
    String path,
    T Function(Map<String, dynamic>) fromJson, {
    Duration? timeout,
  }) =>
      _execute('GET', path, null, fromJson, timeout: timeout);

  Future<Result<T>> post<T>(
    String path,
    Map<String, dynamic> body,
    T Function(Map<String, dynamic>) fromJson, {
    Duration? timeout,
  }) =>
      _execute('POST', path, body, fromJson, timeout: timeout);

  Future<Result<T>> put<T>(
    String path,
    Map<String, dynamic> body,
    T Function(Map<String, dynamic>) fromJson, {
    Duration? timeout,
  }) =>
      _execute('PUT', path, body, fromJson, timeout: timeout);

  Future<Result<T>> delete<T>(
    String path,
    T Function(Map<String, dynamic>) fromJson, {
    Duration? timeout,
  }) =>
      _execute('DELETE', path, null, fromJson, timeout: timeout);

  Future<Result<T>> _execute<T>(
    String method,
    String path,
    Map<String, dynamic>? body,
    T Function(Map<String, dynamic>) fromJson, {
    Duration? timeout,
  }) async {
    if (!await _connectivity.isConnected) {
      return Result.failure(AppException.networkUnavailable());
    }

    return LatencyTracker.track('api_${method.toLowerCase()}_$path', () async {
      return _executeWithRetry(method, path, body, fromJson, 0, timeout: timeout);
    });
  }

  Future<Result<T>> _executeWithRetry<T>(
    String method,
    String path,
    Map<String, dynamic>? body,
    T Function(Map<String, dynamic>) fromJson,
    int attempt, {
    Duration? timeout,
  }) async {
    final url = Uri.parse('${Environment.current.apiBaseUrl}$path');
    final headers = await _headers();
    final stopwatch = Stopwatch()..start();
    final effectiveTimeout = timeout ?? AppConstants.apiReadTimeout;

    try {
      http.Response response;

      switch (method) {
        case 'GET':
          response = await http.get(url, headers: headers).timeout(effectiveTimeout);
        case 'POST':
          response = await http
              .post(url, headers: headers, body: jsonEncode(body))
              .timeout(effectiveTimeout);
        case 'PUT':
          response = await http
              .put(url, headers: headers, body: jsonEncode(body))
              .timeout(effectiveTimeout);
        case 'DELETE':
          response = await http.delete(url, headers: headers).timeout(effectiveTimeout);
        default:
          throw UnsupportedError('Unsupported HTTP method: $method');
      }

      stopwatch.stop();
      AppLogger.network(method, url.toString(), response.statusCode, stopwatch.elapsed);

      if (response.statusCode >= 200 && response.statusCode < 300) {
        final bodyText = response.body.trim();
        final json = bodyText.isEmpty
            ? <String, dynamic>{}
            : jsonDecode(bodyText) as Map<String, dynamic>;
        return Result.success(fromJson(json));
      }

      final networkEx = NetworkException.fromStatusCode(response.statusCode, response.body);

      if (networkEx.isRetryable && attempt < AppConstants.maxApiRetries - 1) {
        await _backoffDelay(attempt);
        return _executeWithRetry(method, path, body, fromJson, attempt + 1, timeout: timeout);
      }

      return Result.failure(networkEx);
    } on SocketException {
      return Result.failure(AppException.networkUnavailable());
    } on TimeoutException {
      return Result.failure(AppException.requestTimeout());
    } on HttpException catch (e) {
      if (attempt < AppConstants.maxApiRetries - 1) {
        await _backoffDelay(attempt);
        return _executeWithRetry(method, path, body, fromJson, attempt + 1, timeout: timeout);
      }
      return Result.failure(AppException.unexpected(e.message, error: e));
    } catch (e, st) {
      AppLogger.error(
        'Unexpected API client failure',
        error: e,
        stackTrace: st,
        tag: 'ApiClient',
        metadata: {'method': method, 'path': path},
      );
      return Result.failure(AppException.unexpected(e.toString(), error: e, stackTrace: st));
    }
  }

  /// Opens a streaming POST, decodes SSE, and yields each raw `data:` payload
  /// string. Strips `data: ` prefix; filters `[DONE]` sentinel and empty lines.
  /// Throws [AppException] / [NetworkException] on non-2xx status.
  Stream<String> streamPost(
    String path,
    Map<String, dynamic> body,
  ) async* {
    final url = Uri.parse('${Environment.current.apiBaseUrl}$path');
    final encodedBody = jsonEncode(body);

    for (var attempt = 0; attempt < AppConstants.maxApiRetries; attempt++) {
      final headers = await _headers();
      headers['Accept'] = 'text/event-stream';
      
      if (!headers.containsKey('Authorization')) {
        throw AppException.sessionTokenUnavailable();
      }
      final request = http.Request('POST', url)
        ..headers.addAll(headers)
        ..body = encodedBody;
      final client = http.Client();
      final stopwatch = Stopwatch()..start();
      var streamAccepted = false;

      try {
        final streamedResponse = await client
            .send(request)
            .timeout(AppConstants.chatStreamConnectTimeout);
        stopwatch.stop();

        AppLogger.network(
          'POST-STREAM',
          url.toString(),
          streamedResponse.statusCode,
          stopwatch.elapsed,
        );

        if (streamedResponse.statusCode < 200 ||
            streamedResponse.statusCode >= 300) {
          final error = NetworkException.fromStatusCode(
            streamedResponse.statusCode,
            '',
          );
          if (error.isRetryable && attempt < AppConstants.maxApiRetries - 1) {
            await _backoffDelay(attempt);
            continue;
          }
          throw error;
        }

        // Once the server has accepted the stream, do not retry. A retry could
        // duplicate tool calls or replay partial assistant text.
        streamAccepted = true;
        await for (final line in streamedResponse.stream
            .transform(utf8.decoder)
            .transform(const LineSplitter())
            .timeout(AppConstants.chatStreamIdleTimeout)) {
          if (line.startsWith('data: ')) {
            final data = line.substring(6).trim();
            if (data.isNotEmpty && data != '[DONE]') yield data;
          }
        }
        return;
      } on SocketException {
        if (!streamAccepted && attempt < AppConstants.maxApiRetries - 1) {
          await _backoffDelay(attempt);
          continue;
        }
        throw AppException.networkUnavailable();
      } on TimeoutException {
        if (!streamAccepted && attempt < AppConstants.maxApiRetries - 1) {
          await _backoffDelay(attempt);
          continue;
        }
        throw AppException.requestTimeout();
      } on HttpException catch (e) {
        if (!streamAccepted && attempt < AppConstants.maxApiRetries - 1) {
          await _backoffDelay(attempt);
          continue;
        }
        throw AppException.unexpected(e.message, error: e);
      } catch (e, st) {
        if (e is AppException) rethrow;
        AppLogger.error(
          'Unexpected streaming API client failure',
          error: e,
          stackTrace: st,
          tag: 'ApiClient',
          metadata: {'path': path, 'attempt': attempt + 1},
        );
        throw AppException.unexpected(e.toString(), error: e, stackTrace: st);
      } finally {
        client.close();
        if (stopwatch.isRunning) stopwatch.stop();
      }
    }
  }

  Future<void> _backoffDelay(int attempt) async {
    final delay = Duration(
      milliseconds: AppConstants.retryBaseDelay.inMilliseconds * (1 << attempt),
    );
    await Future<void>.delayed(delay);
  }
}
