import 'dart:io';

import 'package:path_provider/path_provider.dart';

import '../../core/logging/app_logger.dart';
import '../../core/network/api_client.dart';

/// On-disk cache of Buddy reading a reminder aloud, for the `buddy` alarm tone.
///
/// Filled when an alarm is ARMED, never when it rings. At ring time the alarm
/// fires from a local OS schedule into a process with no Flutter engine, and the
/// phone may be in airplane mode, so anything that needed the network then would
/// simply be silence.
///
/// Files live in the application support directory rather than the temporary
/// one. `getTemporaryDirectory()` is what [GetBetterImageCache] uses and is
/// right for a picture that can be re-fetched on sight, but the OS evicts it
/// under storage pressure and an evicted 3 AM wake line has no second chance.
///
/// Every method fails soft and returns null. The caller arms the alarm either
/// way; a missing clip means the tone loops without ever pausing to speak, which
/// still wakes the user.
class AlarmVoiceCache {
  AlarmVoiceCache({required ApiClient apiClient}) : _apiClient = apiClient;

  final ApiClient _apiClient;

  static const String _tag = 'AlarmVoiceCache';
  static const String _directoryName = 'alarm_voice';

  /// Generous: this runs while the app is in the foreground with time to spare,
  /// and a slow render that eventually lands beats a fast failure that leaves
  /// the alarm mute.
  static const Duration _fetchTimeout = Duration(seconds: 25);

  /// Reconcile can be triggered by startup and resume close together. Collapse
  /// concurrent misses for the same stable key into one HTTP render.
  final Map<String, Future<String?>> _inFlight = {};

  /// The cached clip for this alarm, fetching it once if it is not held yet.
  ///
  /// [clipTag] comes from the server and is stable for message + resolved voice.
  /// It is the whole reason this is cheap. `reconcile()` runs on every app
  /// resume, so without a stable tag every resume would bill a fresh render.
  Future<String?> ensureClip({
    required String reminderId,
    required String clipTag,
  }) async {
    if (reminderId.isEmpty || clipTag.isEmpty) return null;

    final File file;
    try {
      file = await _fileFor(reminderId, clipTag);
    } catch (e) {
      AppLogger.warning('could not resolve the clip directory: $e', tag: _tag);
      return null;
    }

    // Non-empty, not merely present: a fetch interrupted mid-write would leave a
    // zero-byte file that exists forever and plays nothing.
    if (await file.exists() && await file.length() > 0) return file.path;

    final key = file.path;
    final existing = _inFlight[key];
    if (existing != null) return existing;

    final fetch = _fetchAndStore(file, reminderId);
    _inFlight[key] = fetch;
    try {
      return await fetch;
    } finally {
      if (identical(_inFlight[key], fetch)) _inFlight.remove(key);
    }
  }

  Future<String?> _fetchAndStore(File file, String reminderId) async {
    final bytes = await _apiClient.getBytes(
      '/reminders/$reminderId/wake-clip',
      timeout: _fetchTimeout,
    );
    if (bytes == null || bytes.isEmpty) {
      AppLogger.info('no wake clip for $reminderId; the tone will loop', tag: _tag);
      return null;
    }

    try {
      // Write to a temporary name and rename into place. A rename is atomic on
      // the same filesystem, so a process death mid-write can never leave a
      // truncated file sitting under a tag that reads as a cache hit.
      final staging = File('${file.path}.part');
      await staging.writeAsBytes(bytes, flush: true);
      await staging.rename(file.path);
    } catch (e) {
      AppLogger.warning('could not store the wake clip: $e', tag: _tag);
      return null;
    }

    AppLogger.info(
      'cached a ${(bytes.length / 1024).toStringAsFixed(1)} KB wake clip for $reminderId',
      tag: _tag,
    );
    return file.path;
  }

  /// Drop clips for alarms that no longer exist, and stale tags for ones that do.
  ///
  /// Called with the full armed set after a reconcile, which is the only moment
  /// this side knows the complete picture. Without it, every edit to an alarm's
  /// message leaves its previous rendering on disk forever.
  Future<void> retainOnly(Map<String, String> tagsByReminderId) async {
    try {
      final directory = await _directory();
      if (!await directory.exists()) return;
      final keep = tagsByReminderId.entries
          .where((entry) => entry.value.isNotEmpty)
          .map((entry) => _fileName(entry.key, entry.value))
          .toSet();
      await for (final entity in directory.list()) {
        if (entity is! File) continue;
        final name = entity.uri.pathSegments.last;
        if (keep.contains(name)) continue;
        await entity.delete();
        AppLogger.info('evicted stale wake clip $name', tag: _tag);
      }
    } catch (e) {
      // Never fatal. Worst case the directory keeps a few orphaned clips, each a
      // few tens of kilobytes, until the next successful pass.
      AppLogger.warning('wake clip eviction failed: $e', tag: _tag);
    }
  }

  Future<Directory> _directory() async {
    final support = await getApplicationSupportDirectory();
    return Directory('${support.path}${Platform.pathSeparator}$_directoryName');
  }

  Future<File> _fileFor(String reminderId, String clipTag) async {
    final directory = await _directory();
    if (!await directory.exists()) await directory.create(recursive: true);
    return File('${directory.path}${Platform.pathSeparator}${_fileName(reminderId, clipTag)}');
  }

  /// Both halves are sanitised because a reminder id reaches this from the
  /// server and a path separator inside one would write outside the directory.
  String _fileName(String reminderId, String clipTag) =>
      'alarm_voice_${_safe(reminderId)}_${_safe(clipTag)}.mp3';

  String _safe(String value) =>
      value.replaceAll(RegExp(r'[^A-Za-z0-9_-]'), '_');
}
