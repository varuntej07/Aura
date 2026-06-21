import 'package:cloud_firestore/cloud_firestore.dart';
import 'package:firebase_core/firebase_core.dart';
import '../../core/errors/app_exception.dart';
import '../../core/logging/app_logger.dart';
import '../../core/logging/latency_tracker.dart';
import '../../core/network/api_response.dart';

class FirestoreService {
  final FirebaseFirestore? _firestore;

  FirestoreService({FirebaseFirestore? firestore})
    : _firestore = firestore ?? _resolveFirestore() {
    if (_firestore != null) {
      _firestore.settings = const Settings(persistenceEnabled: true);
    }
  }

  static FirebaseFirestore? _resolveFirestore() {
    try {
      if (Firebase.apps.isEmpty) return null;
      return FirebaseFirestore.instance;
    } catch (_) {
      return null;
    }
  }

  Future<Result<T>> getDocument<T>(
    String collection,
    String docId,
    T Function(Map<String, dynamic>) fromJson,
  ) async {
    final firestore = _firestore;
    if (firestore == null) {
      return Result.failure(_firebaseUnavailable());
    }

    return LatencyTracker.track('firestore_get_${collection}_$docId', () async {
      try {
        final doc = await firestore.collection(collection).doc(docId).get();
        if (!doc.exists || doc.data() == null) {
          return Result.failure(
            AppException(
              code: ErrorCode.documentNotFound,
              message: 'Document $docId not found in $collection',
            ),
          );
        }
        final data = {'id': doc.id, ...doc.data()!};
        return Result.success(fromJson(data));
      } catch (e, st) {
        AppLogger.error(
          'Firestore get failed',
          error: e,
          stackTrace: st,
          tag: 'FirestoreService',
        );
        return Result.failure(AppException.firestoreRead(e, st));
      }
    });
  }

  Future<Result<List<T>>> getCollection<T>(
    String collection,
    T Function(Map<String, dynamic>) fromJson, {
    Query<Map<String, dynamic>> Function(
      CollectionReference<Map<String, dynamic>>,
    )?
    queryBuilder,
  }) async {
    final firestore = _firestore;
    if (firestore == null) {
      return Result.failure(_firebaseUnavailable());
    }

    return LatencyTracker.track('firestore_collection_$collection', () async {
      try {
        final ref = firestore.collection(collection);
        final query = queryBuilder != null ? queryBuilder(ref) : ref;
        final snapshot = await query.get();
        final items = snapshot.docs
            .map((doc) => fromJson({'id': doc.id, ...doc.data()}))
            .toList();
        return Result.success(items);
      } catch (e, st) {
        AppLogger.error(
          'Firestore collection read failed',
          error: e,
          stackTrace: st,
          tag: 'FirestoreService',
        );
        return Result.failure(AppException.firestoreRead(e, st));
      }
    });
  }

  Future<Result<T>> setDocument<T>(
    String collection,
    String docId,
    Map<String, dynamic> data,
    T Function(Map<String, dynamic>) fromJson, {
    bool merge = true,
    bool readBack = true,
  }) async {
    final firestore = _firestore;
    if (firestore == null) {
      return Result.failure(_firebaseUnavailable());
    }

    return LatencyTracker.track('firestore_set_${collection}_$docId', () async {
      try {
        await firestore
            .collection(collection)
            .doc(docId)
            .set(data, SetOptions(merge: merge));
        // Write-only paths (e.g. the feedback sink, whose security rules allow
        // create but deny read) must NOT read the doc back: the denied read
        // would throw and surface as a FALSE write failure even though the write
        // already succeeded. Such callers pass readBack: false and we echo the
        // data we just wrote instead of re-fetching it.
        if (!readBack) {
          return Result.success(fromJson(data));
        }
        final result = await getDocument(collection, docId, fromJson);
        return result;
      } catch (e, st) {
        AppLogger.error(
          'Firestore set failed',
          error: e,
          stackTrace: st,
          tag: 'FirestoreService',
        );
        return Result.failure(AppException.firestoreWrite(e, st));
      }
    });
  }

  Future<Result<void>> updateDocument(
    String collection,
    String docId,
    Map<String, dynamic> data,
  ) async {
    final firestore = _firestore;
    if (firestore == null) {
      return Result.failure(_firebaseUnavailable());
    }

    return LatencyTracker.track(
      'firestore_update_${collection}_$docId',
      () async {
        try {
          await firestore.collection(collection).doc(docId).update(data);
          return const Result.success(null);
        } catch (e, st) {
          AppLogger.error(
            'Firestore update failed',
            error: e,
            stackTrace: st,
            tag: 'FirestoreService',
          );
          return Result.failure(AppException.firestoreWrite(e, st));
        }
      },
    );
  }

  Future<Result<void>> deleteDocument(String collection, String docId) async {
    final firestore = _firestore;
    if (firestore == null) {
      return Result.failure(_firebaseUnavailable());
    }

    return LatencyTracker.track(
      'firestore_delete_${collection}_$docId',
      () async {
        try {
          await firestore.collection(collection).doc(docId).delete();
          return const Result.success(null);
        } catch (e, st) {
          AppLogger.error(
            'Firestore delete failed',
            error: e,
            stackTrace: st,
            tag: 'FirestoreService',
          );
          return Result.failure(AppException.firestoreWrite(e, st));
        }
      },
    );
  }

  /// Fetches one page of documents, returning parsed items plus the last
  /// [DocumentSnapshot] to use as a cursor for the next page.
  Future<Result<({List<T> items, DocumentSnapshot? cursor})>>
      getPaginatedCollection<T>(
    String collection,
    T Function(Map<String, dynamic>) fromJson, {
    Query<Map<String, dynamic>> Function(
      CollectionReference<Map<String, dynamic>>,
    )? queryBuilder,
    DocumentSnapshot? after,
    required int limit,
  }) async {
    final firestore = _firestore;
    if (firestore == null) {
      return Result.failure(_firebaseUnavailable());
    }

    return LatencyTracker.track('firestore_page_$collection', () async {
      try {
        final ref = firestore.collection(collection);
        Query<Map<String, dynamic>> query =
            queryBuilder != null ? queryBuilder(ref) : ref;
        if (after != null) query = query.startAfterDocument(after);
        query = query.limit(limit);
        final snapshot = await query.get();
        final items = snapshot.docs
            .map((doc) => fromJson({'id': doc.id, ...doc.data()}))
            .toList();
        final cursor =
            snapshot.docs.isNotEmpty ? snapshot.docs.last : null;
        return Result.success((items: items, cursor: cursor));
      } catch (e, st) {
        AppLogger.error(
          'Firestore paginated read failed',
          error: e,
          stackTrace: st,
          tag: 'FirestoreService',
        );
        return Result.failure(AppException.firestoreRead(e, st));
      }
    });
  }

  Future<Result<void>> batchWrite(
    List<({String collection, String docId, Map<String, dynamic> data})> writes,
  ) async {
    final firestore = _firestore;
    if (firestore == null) {
      return Result.failure(_firebaseUnavailable());
    }

    return LatencyTracker.track('firestore_batch_write', () async {
      try {
        final batch = firestore.batch();
        for (final write in writes) {
          final ref = firestore.collection(write.collection).doc(write.docId);
          batch.set(ref, write.data, SetOptions(merge: true));
        }
        await batch.commit();
        return const Result.success(null);
      } catch (e, st) {
        AppLogger.error(
          'Firestore batch write failed',
          error: e,
          stackTrace: st,
          tag: 'FirestoreService',
        );
        return Result.failure(AppException.firestoreWrite(e, st));
      }
    });
  }

  Stream<T?> documentStream<T>(
    String collection,
    String docId,
    T Function(Map<String, dynamic>) fromJson,
  ) {
    final firestore = _firestore;
    if (firestore == null) {
      return Stream<T?>.value(null);
    }

    return firestore.collection(collection).doc(docId).snapshots().map((snap) {
      if (!snap.exists || snap.data() == null) return null;
      return fromJson({'id': snap.id, ...snap.data()!});
    });
  }

  AppException _firebaseUnavailable() {
    return AppException.unexpected(
      'Cloud Firestore is not configured for this build.',
    );
  }
}
