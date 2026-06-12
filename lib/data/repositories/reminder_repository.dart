import 'package:cloud_firestore/cloud_firestore.dart';

import '../../core/constants/app_constants.dart';
import '../../core/network/api_response.dart';
import '../models/reminder_model.dart';
import '../services/firestore_service.dart';

class ReminderRepository {
  final FirestoreService _firestoreService;

  ReminderRepository({required FirestoreService firestoreService})
      : _firestoreService = firestoreService;

  // ── Pagination state ────────────────────────────────────────────────────────
  // Kept here so the ViewModel never needs to touch Firestore types directly.

  DocumentSnapshot? _cursor;
  bool _hasMore = true;

  bool get hasMore => _hasMore;

  /// Resets cursor and availability flag. Call before the first page load
  /// (e.g. on screen open or pull-to-refresh).
  void resetPagination() {
    _cursor = null;
    _hasMore = true;
  }

  // ── Collection path ─────────────────────────────────────────────────────────

  String _userCollection(String userId) =>
      '${AppConstants.usersCollection}/$userId/${AppConstants.remindersCollection}';

  // ── Paginated read ──────────────────────────────────────────────────────────

  /// Loads the next page of reminders ordered by [trigger_at].
  /// Returns an empty success list when [hasMore] is already false.
  /// Page size defaults to 20; a result shorter than [pageSize] marks the
  /// end of the collection and sets [hasMore] to false.
  Future<Result<List<ReminderModel>>> getNextPage(
    String userId, {
    int pageSize = 20,
  }) async {
    if (!_hasMore) return const Result.success([]);

    final result = await _firestoreService.getPaginatedCollection(
      _userCollection(userId),
      ReminderModel.fromJson,
      queryBuilder: (ref) => ref.orderBy('trigger_at', descending: true),
      after: _cursor,
      limit: pageSize,
    );

    late final Result<List<ReminderModel>> mapped;
    result.when(
      success: (page) {
        _cursor = page.cursor;
        _hasMore = page.items.length >= pageSize;
        mapped = Result.success(page.items);
      },
      failure: (error) {
        mapped = Result.failure(error);
      },
    );
    return mapped;
  }

  // ── Non-paginated helpers (kept for internal / tool-executor use) ───────────

  Future<Result<List<ReminderModel>>> getReminders(
    String userId, {
    ReminderStatus? status,
  }) async {
    return _firestoreService.getCollection(
      _userCollection(userId),
      ReminderModel.fromJson,
      queryBuilder: status != null
          ? (ref) => ref
              .where('status', isEqualTo: status.name)
              .orderBy('trigger_at')
          : (ref) => ref.orderBy('trigger_at'),
    );
  }

  Future<Result<ReminderModel>> getReminder(
    String userId,
    String reminderId,
  ) async {
    return _firestoreService.getDocument(
      _userCollection(userId),
      reminderId,
      ReminderModel.fromJson,
    );
  }

  Future<Result<ReminderModel>> saveReminder(
    String userId,
    ReminderModel reminder,
  ) async {
    final data = reminder.toJson();
    data.remove('id');
    return _firestoreService.setDocument(
      _userCollection(userId),
      reminder.id,
      data,
      ReminderModel.fromJson,
    );
  }

  /// Updates a reminder's status and optional timestamp fields.
  ///
  /// Pass [clearDismissedAt] = true to explicitly remove the `dismissed_at`
  /// field from Firestore (e.g. when reverting a completed reminder).
  Future<Result<void>> updateStatus(
    String userId,
    String reminderId,
    ReminderStatus status, {
    DateTime? firedAt,
    DateTime? dismissedAt,
    bool clearDismissedAt = false,
  }) async {
    final updates = <String, dynamic>{'status': status.name};
    if (firedAt != null) {
      updates['fired_at'] = firedAt.toUtc().toIso8601String();
    }
    if (dismissedAt != null) {
      updates['dismissed_at'] = dismissedAt.toUtc().toIso8601String();
    } else if (clearDismissedAt) {
      updates['dismissed_at'] = FieldValue.delete();
    }
    return _firestoreService.updateDocument(
      _userCollection(userId),
      reminderId,
      updates,
    );
  }

  Future<Result<void>> deleteReminder(
    String userId,
    String reminderId,
  ) async {
    return _firestoreService.deleteDocument(_userCollection(userId), reminderId);
  }
}
