import 'package:device_info_plus/device_info_plus.dart';
import 'package:flutter/foundation.dart';
import 'package:package_info_plus/package_info_plus.dart';

import '../../core/logging/app_logger.dart';

/// Collects everything the client can know about the device, install, and
/// region, for stamping onto the `users/{uid}` doc alongside the existing
/// login metadata (platform, sign_in_method, timezone).
///
/// Every read degrades independently: a plugin failure drops that group of
/// fields rather than blocking sign-in, so the doc converges to whatever the
/// device can actually report. The result is cached per app run because none
/// of it changes while the process is alive.
class DeviceMetadataService {
  // Field names on users/{uid}. The backend/ops console are the readers;
  // keep writer and readers on these constants.
  static const String fieldAppVersion = 'app_version';
  static const String fieldInstallStore = 'install_store';
  static const String fieldFirstInstalledAt = 'first_installed_at';
  static const String fieldAppUpdatedAt = 'app_updated_at';
  static const String fieldDeviceLocale = 'device_locale';
  static const String fieldDeviceCountry = 'device_country';
  static const String fieldDevice = 'device';

  /// Sentinel written when the OS reports no installer package: on Android
  /// that means the APK did not come from a store (sideload / ADB install).
  static const String installStoreSideload = 'sideload';

  final DeviceInfoPlugin _deviceInfoPlugin;

  Map<String, dynamic>? _cachedMetadata;

  DeviceMetadataService({DeviceInfoPlugin? deviceInfoPlugin})
      : _deviceInfoPlugin = deviceInfoPlugin ?? DeviceInfoPlugin();

  /// Returns the metadata fields to merge into the user-doc write.
  /// Never throws; a total failure returns an empty map.
  Future<Map<String, dynamic>> collect() async {
    final cached = _cachedMetadata;
    if (cached != null) return cached;

    final metadata = <String, dynamic>{
      ..._localeFields(),
      ...await _packageFields(),
      ...await _deviceFields(),
    };
    _cachedMetadata = metadata;
    return metadata;
  }

  /// OS-level locale, the finest region signal the client has without a
  /// server-side IP lookup (e.g. "en-IN" -> country "IN").
  Map<String, dynamic> _localeFields() {
    try {
      final locale = PlatformDispatcher.instance.locale;
      return {
        fieldDeviceLocale: locale.toLanguageTag(),
        if (locale.countryCode != null) fieldDeviceCountry: locale.countryCode,
      };
    } catch (error) {
      AppLogger.warning('Could not read device locale: $error',
          tag: 'DeviceMetadataService');
      return const {};
    }
  }

  /// App version, where the install came from, and install/update times.
  Future<Map<String, dynamic>> _packageFields() async {
    try {
      final packageInfo = await PackageInfo.fromPlatform();
      return {
        fieldAppVersion:
            '${packageInfo.version}+${packageInfo.buildNumber}',
        fieldInstallStore: packageInfo.installerStore ?? installStoreSideload,
        if (packageInfo.installTime != null)
          fieldFirstInstalledAt:
              packageInfo.installTime!.toUtc().toIso8601String(),
        if (packageInfo.updateTime != null)
          fieldAppUpdatedAt: packageInfo.updateTime!.toUtc().toIso8601String(),
      };
    } catch (error) {
      AppLogger.warning('Could not read package info: $error',
          tag: 'DeviceMetadataService');
      return const {};
    }
  }

  /// Hardware and OS identity as one `device` map field, so the user doc
  /// stays tidy and the whole group is queryable via dotted paths
  /// (e.g. `device.os_version`).
  Future<Map<String, dynamic>> _deviceFields() async {
    try {
      switch (defaultTargetPlatform) {
        case TargetPlatform.android:
          final info = await _deviceInfoPlugin.androidInfo;
          return {
            fieldDevice: {
              'manufacturer': info.manufacturer,
              'brand': info.brand,
              'model': info.model,
              'os': 'android',
              'os_version': info.version.release,
              'sdk_int': info.version.sdkInt,
              'is_physical_device': info.isPhysicalDevice,
            },
          };
        case TargetPlatform.iOS:
          final info = await _deviceInfoPlugin.iosInfo;
          return {
            fieldDevice: {
              'manufacturer': 'Apple',
              'model': info.utsname.machine,
              'os': 'ios',
              'os_version': info.systemVersion,
              'is_physical_device': info.isPhysicalDevice,
            },
          };
        default:
          return {
            fieldDevice: {'os': defaultTargetPlatform.name},
          };
      }
    } catch (error) {
      AppLogger.warning('Could not read device info: $error',
          tag: 'DeviceMetadataService');
      return const {};
    }
  }
}
