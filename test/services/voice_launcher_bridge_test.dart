import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:aura/data/services/voice_launcher_bridge.dart';

/// VoiceLauncherBridge is the Dart half of the home-screen voice widget. The
/// method names and the "voice" action string here must stay in lockstep with
/// the Kotlin side (MainActivity.LAUNCH_ACTION_VOICE + the `dev.varuntej.aura/widget`
/// channel handler). A drift here means a widget tap silently does nothing, so
/// these tests pin the contract.
void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  const channel = MethodChannel('dev.varuntej.aura/widget');
  final messenger =
      TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger;
  final calls = <MethodCall>[];

  setUp(() {
    // The native side only answers on Android; force it so the gated paths run.
    debugDefaultTargetPlatformOverride = TargetPlatform.android;
    calls.clear();
    messenger.setMockMethodCallHandler(channel, (call) async {
      calls.add(call);
      switch (call.method) {
        case 'consumeLaunchAction':
          return 'voice';
        case 'requestPinVoiceWidget':
        case 'isPinVoiceWidgetSupported':
          return true;
      }
      return null;
    });
  });

  tearDown(() {
    messenger.setMockMethodCallHandler(channel, null);
    debugDefaultTargetPlatformOverride = null;
  });

  test('launchActionVoice matches the native LAUNCH_ACTION_VOICE constant', () {
    expect(VoiceLauncherBridge.launchActionVoice, 'voice');
  });

  test('consumePendingLaunchAction reads the cold-launch action', () async {
    final action =
        await VoiceLauncherBridge.instance.consumePendingLaunchAction();

    expect(action, 'voice');
    expect(calls.single.method, 'consumeLaunchAction');
  });

  test('requestPinVoiceWidget asks the launcher to pin the widget', () async {
    final ok = await VoiceLauncherBridge.instance.requestPinVoiceWidget();

    expect(ok, isTrue);
    expect(calls.single.method, 'requestPinVoiceWidget');
  });

  test('a native onLaunchAction call surfaces on the launchActions stream',
      () async {
    VoiceLauncherBridge.instance.start();

    final next = VoiceLauncherBridge.instance.launchActions.first;
    await messenger.handlePlatformMessage(
      channel.name,
      const StandardMethodCodec()
          .encodeMethodCall(const MethodCall('onLaunchAction', 'voice')),
      (_) {},
    );

    expect(await next, 'voice');
  });
}
