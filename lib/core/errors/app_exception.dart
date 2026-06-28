enum ErrorCode {
  // Network
  networkUnavailable,
  requestTimeout,
  serverError,
  unauthorized,
  forbidden,
  notFound,
  // Auth
  authFailed,
  authCancelled,
  authTokenExpired,
  // Firestore
  firestoreReadFailed,
  firestoreWriteFailed,
  documentNotFound,
  // Generic
  unexpected,
  unknown,
}

class AppException implements Exception {
  final ErrorCode code;
  final String message;
  final Object? originalError;
  final StackTrace? stackTrace;

  const AppException({
    required this.code,
    required this.message,
    this.originalError,
    this.stackTrace,
  });

  factory AppException.unexpected(String message, {Object? error, StackTrace? stackTrace}) {
    return AppException(
      code: ErrorCode.unexpected,
      message: message,
      originalError: error,
      stackTrace: stackTrace,
    );
  }

  factory AppException.networkUnavailable() {
    return const AppException(
      code: ErrorCode.networkUnavailable,
      message: "Couldn't reach Buddy. Check your connection and try again.",
    );
  }

  /// A request failed in transit (dropped or reset stream, server closed the
  /// connection, unreachable host) while the device still has connectivity. This
  /// is NOT the user's network, so we never tell an online user to "check your connection".
  factory AppException.connectionInterrupted() {
    return const AppException(
      code: ErrorCode.serverError,
      message: "Couldn't reach Buddy just now. Give it another try.",
    );
  }

  factory AppException.unauthorized() {
    return const AppException(
      code: ErrorCode.unauthorized,
      message: "Sign-in didn't work. Please try again.",
    );
  }

  /// A signed-in user's Firebase ID token could not be fetched (expired cache +
  /// a failed refresh, e.g. a cold launch from a notification tap on a flaky network).
  factory AppException.sessionTokenUnavailable() {
    return const AppException(
      code: ErrorCode.authTokenExpired,
      message: "Couldn't verify your session. Try again in a moment.",
    );
  }

  factory AppException.serverError(int statusCode, String body) {
    return AppException(
      code: ErrorCode.serverError,
      message: "Something went wrong on Buddy's end. Try again in a moment.",
      originalError: 'Server error ($statusCode): $body',
    );
  }

  factory AppException.requestTimeout() {
    return const AppException(
      code: ErrorCode.requestTimeout,
      message: "Buddy took too long to respond. Mind trying again?",
    );
  }

  factory AppException.firestoreRead(Object error, [StackTrace? st]) {
    return AppException(
      code: ErrorCode.firestoreReadFailed,
      message: 'Something went wrong. Try again in a moment.',
      originalError: error,
      stackTrace: st,
    );
  }

  factory AppException.firestoreWrite(Object error, [StackTrace? st]) {
    return AppException(
      code: ErrorCode.firestoreWriteFailed,
      message: 'Something went wrong. Try again in a moment.',
      originalError: error,
      stackTrace: st,
    );
  }

  factory AppException.authFailed(Object error, [StackTrace? st]) {
    return AppException(
      code: ErrorCode.authFailed,
      message: "Sign-in didn't work. Please try again.",
      originalError: error,
      stackTrace: st,
    );
  }

  factory AppException.authCancelled() {
    return const AppException(
      code: ErrorCode.authCancelled,
      message: 'Sign-in cancelled.',
    );
  }

  @override
  String toString() => 'AppException(${code.name}): $message';
}
