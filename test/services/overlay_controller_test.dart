import 'package:flutter_test/flutter_test.dart';

import 'package:aura/data/services/desktop/overlay_controller.dart';

/// Every edge in the overlay state diagram (plan: distributed-humming-perlis,
/// review decision 6). The invariant under test throughout: an active voice
/// session is never hidden.
void main() {
  group('OverlayController', () {
    test('starts hidden with voice inactive', () {
      final controller = OverlayController();
      expect(controller.presentation, OverlayPresentation.hidden);
      expect(controller.voiceActive, isFalse);
    });

    test('hotkey toggles hidden -> panel -> hidden', () {
      final controller = OverlayController();
      controller.hotkeyPressed();
      expect(controller.presentation, OverlayPresentation.panel);
      controller.hotkeyPressed();
      expect(controller.presentation, OverlayPresentation.hidden);
    });

    test('summon lands on panel and is idempotent', () {
      final controller = OverlayController();
      controller.summon();
      controller.summon();
      expect(controller.presentation, OverlayPresentation.panel);
    });

    test('focus loss while idle hides', () {
      final controller = OverlayController()..summon();
      controller.focusLost();
      expect(controller.presentation, OverlayPresentation.hidden);
    });

    test('focus loss during active voice collapses to pill, voice continues',
        () {
      final controller = OverlayController()..summon();
      controller.setVoiceActive(true);
      controller.focusLost();
      expect(controller.presentation, OverlayPresentation.pill);
      expect(controller.voiceActive, isTrue);
    });

    test('focus loss while already pill is a no-op', () {
      final controller = OverlayController()..summon();
      controller.setVoiceActive(true);
      controller.focusLost();
      controller.focusLost();
      expect(controller.presentation, OverlayPresentation.pill);
    });

    test('pill click restores the panel without ending voice', () {
      final controller = OverlayController()..summon();
      controller.setVoiceActive(true);
      controller.focusLost();
      controller.pillActivated();
      expect(controller.presentation, OverlayPresentation.panel);
      expect(controller.voiceActive, isTrue);
    });

    test('hotkey from pill restores the panel without ending voice', () {
      final controller = OverlayController()..summon();
      controller.setVoiceActive(true);
      controller.focusLost();
      controller.hotkeyPressed();
      expect(controller.presentation, OverlayPresentation.panel);
      expect(controller.voiceActive, isTrue);
    });

    test('esc always ends the session and hides', () {
      var endVoiceCalls = 0;
      final controller = OverlayController()..summon();
      controller.onEndVoiceSession = () => endVoiceCalls++;
      controller.setVoiceActive(true);
      controller.escPressed();
      expect(controller.presentation, OverlayPresentation.hidden);
      expect(controller.voiceActive, isFalse);
      expect(endVoiceCalls, 1);
    });

    test('hotkey dismiss from panel ends an active session', () {
      var endVoiceCalls = 0;
      final controller = OverlayController()..summon();
      controller.onEndVoiceSession = () => endVoiceCalls++;
      controller.setVoiceActive(true);
      controller.hotkeyPressed();
      expect(controller.presentation, OverlayPresentation.hidden);
      expect(endVoiceCalls, 1);
    });

    test('esc while idle just hides, no end-voice callback', () {
      var endVoiceCalls = 0;
      final controller = OverlayController()..summon();
      controller.onEndVoiceSession = () => endVoiceCalls++;
      controller.escPressed();
      expect(controller.presentation, OverlayPresentation.hidden);
      expect(endVoiceCalls, 0);
    });

    test('invariant: voice starting while hidden forces the panel up', () {
      final controller = OverlayController();
      controller.setVoiceActive(true);
      expect(controller.presentation, OverlayPresentation.panel);
    });

    test('session ending while collapsed lets the pill vanish', () {
      final controller = OverlayController()..summon();
      controller.setVoiceActive(true);
      controller.focusLost();
      controller.setVoiceActive(false);
      expect(controller.presentation, OverlayPresentation.hidden);
    });

    test('session ending while panel is visible keeps the panel', () {
      final controller = OverlayController()..summon();
      controller.setVoiceActive(true);
      controller.setVoiceActive(false);
      expect(controller.presentation, OverlayPresentation.panel);
    });
  });

  group('OverlayController panel variant', () {
    test('defaults to the setup sheet', () {
      final controller = OverlayController();
      expect(controller.panelVariant, OverlayPanelVariant.setup);
    });

    test('setPanelVariant notifies so the window resizes mid-panel', () {
      var notifications = 0;
      final controller = OverlayController()..summon();
      controller.addListener(() => notifications++);
      controller.setPanelVariant(OverlayPanelVariant.bar);
      expect(controller.panelVariant, OverlayPanelVariant.bar);
      expect(notifications, 1);
    });

    test('setting the same variant again is silent', () {
      var notifications = 0;
      final controller = OverlayController();
      controller.addListener(() => notifications++);
      controller.setPanelVariant(OverlayPanelVariant.setup);
      expect(notifications, 0);
    });

    test('variant survives presentation transitions', () {
      final controller = OverlayController()
        ..setPanelVariant(OverlayPanelVariant.bar)
        ..summon();
      controller.focusLost();
      controller.summon();
      expect(controller.panelVariant, OverlayPanelVariant.bar);
    });
  });

  group('OverlayController onboarding step', () {
    test('defaults to welcome', () {
      final controller = OverlayController();
      expect(controller.onboardingStep, DesktopOnboardingStep.welcome);
    });

    test('setOnboardingStep notifies so the window resizes mid-panel', () {
      var notifications = 0;
      final controller = OverlayController()..summon();
      controller.addListener(() => notifications++);
      controller.setOnboardingStep(DesktopOnboardingStep.getApp);
      expect(controller.onboardingStep, DesktopOnboardingStep.getApp);
      expect(notifications, 1);
    });

    test('setting the same step again is silent', () {
      var notifications = 0;
      final controller = OverlayController();
      controller.addListener(() => notifications++);
      controller.setOnboardingStep(DesktopOnboardingStep.welcome);
      expect(notifications, 0);
    });
  });

  group('OverlayController measured setup height', () {
    test('starts unmeasured', () {
      final controller = OverlayController();
      expect(controller.measuredSetupHeight, isNull);
    });

    test('reportMeasuredSetupHeight stores and notifies', () {
      var notifications = 0;
      final controller = OverlayController();
      controller.addListener(() => notifications++);
      controller.reportMeasuredSetupHeight(310);
      expect(controller.measuredSetupHeight, 310);
      expect(notifications, 1);
    });

    test('a sub-pixel-jitter change is silent (epsilon)', () {
      var notifications = 0;
      final controller = OverlayController()..reportMeasuredSetupHeight(310);
      controller.addListener(() => notifications++);
      controller.reportMeasuredSetupHeight(310.4);
      expect(notifications, 0);
    });

    test('a real change past the epsilon notifies again', () {
      var notifications = 0;
      final controller = OverlayController()..reportMeasuredSetupHeight(310);
      controller.addListener(() => notifications++);
      controller.reportMeasuredSetupHeight(340);
      expect(controller.measuredSetupHeight, 340);
      expect(notifications, 1);
    });

    test('clamps to the sane min/max range', () {
      final controller = OverlayController();
      controller.reportMeasuredSetupHeight(20);
      expect(controller.measuredSetupHeight, 120);
      controller.reportMeasuredSetupHeight(9000);
      expect(controller.measuredSetupHeight, 560);
    });
  });

  group('OverlayController pointing', () {
    test('start remembers the prior surface and end restores it', () {
      final controller = OverlayController()..summon();
      controller.startPointing();
      expect(controller.presentation, OverlayPresentation.pointing);
      controller.endPointing();
      expect(controller.presentation, OverlayPresentation.panel);
    });

    test('pointing from the pill returns to the pill while voice lives', () {
      final controller = OverlayController()..summon();
      controller.setVoiceActive(true);
      controller.focusLost(); // -> pill
      controller.startPointing();
      controller.endPointing();
      expect(controller.presentation, OverlayPresentation.pill);
    });

    test('a stale pill (voice died mid-flight) collapses to hidden', () {
      final controller = OverlayController()..summon();
      controller.setVoiceActive(true);
      controller.focusLost(); // -> pill
      controller.startPointing();
      controller.setVoiceActive(false);
      controller.endPointing();
      // Restoring a pill with no live mic would fake the mic-live indicator.
      expect(controller.presentation, OverlayPresentation.hidden);
    });

    test('focus loss during pointing is a no-op (click-through by design)',
        () {
      final controller = OverlayController()..summon();
      controller.startPointing();
      controller.focusLost();
      expect(controller.presentation, OverlayPresentation.pointing);
    });

    test('hotkey during pointing fires the cancel handler, nothing else', () {
      var cancelCalls = 0;
      final controller = OverlayController()..summon();
      controller.onCancelPointing = () => cancelCalls++;
      controller.startPointing();
      controller.hotkeyPressed();
      expect(cancelCalls, 1);
      // The cancel handler (the pointing service) owns the restore.
      expect(controller.presentation, OverlayPresentation.pointing);
    });

    test('esc during pointing cancels the flight and ends everything', () {
      var cancelCalls = 0;
      final controller = OverlayController()..summon();
      controller.onCancelPointing = () => cancelCalls++;
      controller.startPointing();
      controller.escPressed();
      expect(cancelCalls, 1);
      expect(controller.presentation, OverlayPresentation.hidden);
    });

    test('voice starting mid-flight does not yank the window', () {
      final controller = OverlayController(); // hidden
      controller.startPointing();
      controller.setVoiceActive(true);
      expect(controller.presentation, OverlayPresentation.pointing);
    });
  });
}
