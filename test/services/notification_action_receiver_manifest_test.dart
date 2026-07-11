import 'dart:io';

import 'package:flutter_test/flutter_test.dart';

/// Guards the AndroidManifest contract behind the thread follow-up reply chips.
///
/// flutter_local_notifications delivers a notification ACTION tap
/// (showsUserInterface: false, the reply-chip path in
/// thread_notification_handler.dart) as a broadcast to its
/// ActionBroadcastReceiver. The plugin does NOT merge that receiver into the
/// app manifest itself; if the declaration is removed, Android silently drops
/// the tap: the shade shows a "sending" spinner forever, the reply never
/// reaches /threads/reply, and nothing errors anywhere. This test makes that
/// silent failure a loud CI failure.
void main() {
  test('AndroidManifest declares the flutter_local_notifications action receiver', () {
    final manifest = File('android/app/src/main/AndroidManifest.xml').readAsStringSync();

    expect(
      manifest,
      contains('com.dexterous.flutterlocalnotifications.ActionBroadcastReceiver'),
      reason: 'Notification reply chips (thread_followup) dispatch through '
          'ActionBroadcastReceiver. Removing its <receiver> declaration makes '
          'every chip tap a silent no-op with a stuck spinner.',
    );
  });
}
