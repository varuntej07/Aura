import '../../core/base/safe_change_notifier.dart';
import '../../core/logging/app_logger.dart';
import '../../data/models/reminder_model.dart';
import '../../data/repositories/reminder_repository.dart';
import 'view_state.dart';

class RemindersViewModel extends SafeChangeNotifier {
  final ReminderRepository _repository;

  RemindersViewModel({required ReminderRepository repository})
      : _repository = repository;

  ViewState _state = ViewState.idle;
  List<ReminderModel> _reminders = [];
  List<ReminderModel> _activeReminders = [];
  List<ReminderModel> _completedReminders = [];
  String? _errorMessage;
  bool _isLoadingMore = false;
  bool _hasLoaded = false;
  String? _loadedForUserId;

  ViewState get state => _state;
  String? get errorMessage => _errorMessage;

  /// True while a subsequent page is being fetched (initial load uses [state]).
  bool get isLoadingMore => _isLoadingMore;

  /// Whether more pages exist in Firestore.
  bool get hasMore => _repository.hasMore;

  /// Derived lists

  /// Pending / snoozed / fired reminders sorted soonest-first.
  List<ReminderModel> get activeReminders => _activeReminders;

  /// Only explicitly dismissed reminders, sorted most-recent-first.
  List<ReminderModel> get completedReminders => _completedReminders;

  void _rebuildDerivedLists() {
    _activeReminders = _reminders.where((r) => r.status.isActive).toList()
      ..sort((a, b) => b.triggerAt.compareTo(a.triggerAt));
    _completedReminders = _reminders.where((r) => r.status.isCompleted).toList()
      ..sort((a, b) => b.triggerAt.compareTo(a.triggerAt));
  }

  /// Loads the first page. No-ops if already loaded for this user.
  /// Call [refreshReminders] to force a reload.
  Future<void> loadReminders(String userId) async {
    if (_hasLoaded && _loadedForUserId == userId) return;
    await _fetchFirstPage(userId);
  }

  /// Forces a full reload from Firestore, discarding cached state.
  Future<void> refreshReminders(String userId) async {
    await _fetchFirstPage(userId);
  }

  Future<void> _fetchFirstPage(String userId) async {
    _state = ViewState.loading;
    _errorMessage = null;
    _reminders = [];
    _isLoadingMore = false;
    _hasLoaded = false;
    _repository.resetPagination();
    _rebuildDerivedLists();
    safeNotifyListeners();

    final result = await _repository.getNextPage(userId);
    result.when(
      success: (items) {
        _reminders = items;
        _hasLoaded = true;
        _loadedForUserId = userId;
        _state = ViewState.loaded;
        _rebuildDerivedLists();
        safeNotifyListeners();
      },
      failure: (error) {
        _errorMessage = 'Something went wrong. Try again in a moment.';
        _state = ViewState.error;
        AppLogger.error(
          'Failed to load reminders',
          error: error,
          tag: 'RemindersVM',
          metadata: {'userId': userId},
        );
        safeNotifyListeners();
      },
    );
  }

  /// Appends the next page. No-ops when already loading or no more pages.
  Future<void> loadMore(String userId) async {
    if (_isLoadingMore || !hasMore || _state != ViewState.loaded) return;

    _isLoadingMore = true;
    safeNotifyListeners();

    final result = await _repository.getNextPage(userId);
    result.when(
      success: (items) {
        _reminders.addAll(items);
        _isLoadingMore = false;
        _rebuildDerivedLists();
        safeNotifyListeners();
      },
      failure: (error) {
        _isLoadingMore = false;
        _errorMessage = 'Something went wrong. Try again in a moment.';
        AppLogger.error(
          'Failed to load more reminders',
          error: error,
          tag: 'RemindersVM',
          metadata: {'userId': userId},
        );
        safeNotifyListeners();
      },
    );
  }

  /// Marks a reminder as dismissed (Google Tasks-style "complete").
  /// Optimistically updates local state so the UI is instant.
  Future<void> markComplete(String userId, String reminderId) async {
    final idx = _reminders.indexWhere((r) => r.id == reminderId);
    if (idx < 0) return;

    final original = _reminders[idx];
    _reminders[idx] = original.copyWith(
      status: ReminderStatus.dismissed,
      dismissedAt: DateTime.now(),
    );
    _rebuildDerivedLists();
    safeNotifyListeners();

    final result = await _repository.updateStatus(
      userId,
      reminderId,
      ReminderStatus.dismissed,
      dismissedAt: DateTime.now(),
    );

    result.when(
      success: (_) {},
      failure: (error) {
        _reminders[idx] = original;
        _errorMessage = 'Something went wrong. Try again in a moment.';
        AppLogger.error(
          'Failed to mark reminder complete',
          error: error,
          tag: 'RemindersVM',
          metadata: {'reminderId': reminderId},
        );
        _rebuildDerivedLists();
        safeNotifyListeners();
      },
    );
  }

  /// Reverts a dismissed reminder back to active — undoing an accidental tap.
  ///
  /// Restores to [ReminderStatus.fired] if the notification was already sent,
  /// otherwise [ReminderStatus.pending], preserving the original timeline.
  Future<void> markIncomplete(String userId, String reminderId) async {
    final idx = _reminders.indexWhere((r) => r.id == reminderId);
    if (idx < 0) return;

    final original = _reminders[idx];
    final restoredStatus = original.firedAt != null
        ? ReminderStatus.fired
        : ReminderStatus.pending;

    _reminders[idx] = original.copyWith(
      status: restoredStatus,
      dismissedAt: null, // sentinel in copyWith handles explicit null
    );
    _rebuildDerivedLists();
    safeNotifyListeners();

    final result = await _repository.updateStatus(
      userId,
      reminderId,
      restoredStatus,
      clearDismissedAt: true,
    );

    result.when(
      success: (_) {},
      failure: (error) {
        _reminders[idx] = original;
        _errorMessage = 'Something went wrong. Try again in a moment.';
        AppLogger.error(
          'Failed to undo reminder completion',
          error: error,
          tag: 'RemindersVM',
          metadata: {'reminderId': reminderId},
        );
        _rebuildDerivedLists();
        safeNotifyListeners();
      },
    );
  }

  void clearError() {
    _errorMessage = null;
    safeNotifyListeners();
  }
}
