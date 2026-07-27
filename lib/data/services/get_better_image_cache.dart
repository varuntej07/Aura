import 'dart:io';

import 'package:http/http.dart' as http;
import 'package:path_provider/path_provider.dart';

import '../../core/logging/app_logger.dart';

/// A fixed editorial image catalog for the Get Better surface.
///
/// The backend selects an allowlisted key instead of returning arbitrary URLs.
/// Each image is downloaded once into the OS cache directory and reused on later
/// opens. A missing image is non-fatal because the UI has a keyed gradient.
class GetBetterImageCache {
  static const Map<String, String> _imageUrls = {
    'momentum':
        'https://images.unsplash.com/photo-1499750310107-5fef28a66643'
        '?auto=format&fit=crop&w=1200&q=82',
    'focus':
        'https://images.unsplash.com/photo-1484480974693-6ca0a78fb36b'
        '?auto=format&fit=crop&w=1200&q=82',
    'calm':
        'https://images.unsplash.com/photo-1500530855697-b586d89ba3ee'
        '?auto=format&fit=crop&w=1200&q=82',
    'learning':
        'https://images.unsplash.com/photo-1516321318423-f06f85e504b3'
        '?auto=format&fit=crop&w=1200&q=82',
    'wellbeing':
        'https://images.unsplash.com/photo-1544367567-0f2fcb009e0b'
        '?auto=format&fit=crop&w=1200&q=82',
    'relationships':
        'https://images.unsplash.com/photo-1529156069898-49953e39b3ac'
        '?auto=format&fit=crop&w=1200&q=82',
    'career':
        'https://images.unsplash.com/photo-1521737711867-e3b97375f902'
        '?auto=format&fit=crop&w=1200&q=82',
    'creativity':
        'https://images.unsplash.com/photo-1513364776144-60967b0f800f'
        '?auto=format&fit=crop&w=1200&q=82',
    'money':
        'https://images.unsplash.com/photo-1579621970563-ebec7560ff3e'
        '?auto=format&fit=crop&w=1200&q=82',
    'routines':
        'https://images.unsplash.com/photo-1483058712412-4245e9b90334'
        '?auto=format&fit=crop&w=1200&q=82',
    'confidence':
        'https://images.unsplash.com/photo-1499209974431-9dddcece7f88'
        '?auto=format&fit=crop&w=1200&q=82',
    'adventure':
        'https://images.unsplash.com/photo-1500534623283-312aade485b7'
        '?auto=format&fit=crop&w=1200&q=82',
  };

  Future<File?> load(String imageKey) async {
    final safeKey = _imageUrls.containsKey(imageKey) ? imageKey : 'momentum';
    try {
      final cacheDirectory = await getTemporaryDirectory();
      final file = File(
        '${cacheDirectory.path}${Platform.pathSeparator}'
        'get_better_$safeKey.jpg',
      );
      if (await file.exists() && await file.length() > 0) {
        return file;
      }

      final response = await http
          .get(Uri.parse(_imageUrls[safeKey]!))
          .timeout(const Duration(seconds: 12));
      if (response.statusCode < 200 || response.statusCode >= 300) {
        AppLogger.warning(
          'Get Better image download returned ${response.statusCode}',
          tag: 'GetBetterImageCache',
          metadata: {'imageKey': safeKey},
        );
        return null;
      }
      await file.writeAsBytes(response.bodyBytes, flush: true);
      return file;
    } catch (error, stackTrace) {
      AppLogger.warning(
        'Get Better image cache miss could not be filled',
        tag: 'GetBetterImageCache',
        metadata: {'imageKey': safeKey, 'error': error.toString()},
      );
      AppLogger.debug(stackTrace.toString(), tag: 'GetBetterImageCache');
      return null;
    }
  }
}
