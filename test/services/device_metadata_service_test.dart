import 'package:flutter_test/flutter_test.dart';

import 'package:aura/data/services/device_metadata_service.dart';

/// In the unit-test environment the platform channels behind package_info_plus
/// and device_info_plus don't exist, which is exactly the degraded path the
/// service must survive: it may only drop field groups, never throw.
void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  test('collect never throws and returns only known field names', () async {
    final service = DeviceMetadataService();

    final metadata = await service.collect();

    const knownFields = {
      DeviceMetadataService.fieldAppVersion,
      DeviceMetadataService.fieldInstallStore,
      DeviceMetadataService.fieldFirstInstalledAt,
      DeviceMetadataService.fieldAppUpdatedAt,
      DeviceMetadataService.fieldDeviceLocale,
      DeviceMetadataService.fieldDeviceCountry,
      DeviceMetadataService.fieldDevice,
    };
    expect(metadata.keys.toSet().difference(knownFields), isEmpty);
  });

  test('collect includes the device locale even when plugins are missing',
      () async {
    final service = DeviceMetadataService();

    final metadata = await service.collect();

    expect(metadata[DeviceMetadataService.fieldDeviceLocale], isNotEmpty);
  });

  test('collect caches its result per app run', () async {
    final service = DeviceMetadataService();

    final first = await service.collect();
    final second = await service.collect();

    expect(identical(first, second), isTrue);
  });
}
