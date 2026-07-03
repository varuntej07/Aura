import 'package:aura/data/services/deep_link_service.dart';
import 'package:aura/data/services/voice_launcher_bridge.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  group('DeepLinkService.actionForUri', () {
    test('aura://voice maps to the voice launch action', () {
      expect(
        DeepLinkService.actionForUri(Uri.parse('aura://voice')),
        VoiceLauncherBridge.launchActionVoice,
      );
    });

    test('https App Link path /voice maps to the voice action', () {
      expect(
        DeepLinkService.actionForUri(Uri.parse('https://auravoiceapp.com/voice')),
        VoiceLauncherBridge.launchActionVoice,
      );
    });

    test('a trailing slash on the App Link still maps', () {
      expect(
        DeepLinkService.actionForUri(Uri.parse('https://auravoiceapp.com/voice/')),
        VoiceLauncherBridge.launchActionVoice,
      );
    });

    test('unrelated links return null', () {
      for (final url in [
        'aura://settings',
        'https://auravoiceapp.com/',
        'https://auravoiceapp.com/privacy-policy',
        'https://example.com/voice',
        'aura://',
      ]) {
        expect(DeepLinkService.actionForUri(Uri.parse(url)), isNull, reason: url);
      }
    });
  });
}
