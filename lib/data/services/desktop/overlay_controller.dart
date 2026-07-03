import 'package:flutter/foundation.dart';

/// What the overlay window should currently present. [pointing] is the
/// fullscreen click-through pointer flight; the pointing service owns the
/// window bounds for its duration, so [DesktopWindowService] must not touch
/// the window while it is active.
enum OverlayPresentation { hidden, panel, pill, pointing }

/// Which shape the panel takes: [setup] is the tall sign-in/onboarding sheet
/// (forms need room), [bar] is the compact signed-in glass bar. Synced from
/// auth state by the overlay app; [DesktopWindowService] sizes the window
/// from it.
enum OverlayPanelVariant { setup, bar }

/// Which step of [DesktopOnboardingFlow] is showing inside the [setup]
/// variant. Each step's content is a different height, so
/// [DesktopWindowService] sizes the window per step instead of one fixed
/// sheet tall enough for all of them (the empty space above/below short
/// steps that prompted this).
enum DesktopOnboardingStep { welcome, getApp, link }

/// Pure state machine for the desktop overlay. Owns WHAT should be on screen;
/// [DesktopWindowService] applies it to the real window, so every transition
/// here is unit-testable without a window.
///
/// Session-aware hide rule (review decision 6): an idle overlay hides on focus
/// loss; during active voice, focus loss collapses to a compact always-on-top
/// pill and the conversation continues; Esc always ends everything.
///
/// Hard invariant: an active voice session is NEVER hidden. The mic being live
/// always has a visible on-screen indicator (panel or pill).
class OverlayController extends ChangeNotifier {
  OverlayPresentation _presentation = OverlayPresentation.hidden;
  OverlayPresentation _beforePointing = OverlayPresentation.hidden;
  bool _voiceActive = false;

  /// Invoked when a transition requires ending the live voice session
  /// (Esc, or hotkey-toggle from the panel). Wired by the voice layer in M2.
  VoidCallback? onEndVoiceSession;

  /// Invoked when a transition must abort an in-flight pointing animation
  /// (hotkey/Esc during pointing). Wired by the pointing service, which owns
  /// the window's click-through state and bounds while pointing.
  VoidCallback? onCancelPointing;

  OverlayPresentation get presentation => _presentation;
  bool get voiceActive => _voiceActive;

  OverlayPanelVariant _panelVariant = OverlayPanelVariant.setup;
  OverlayPanelVariant get panelVariant => _panelVariant;

  /// Auth state drives the panel shape: signed out shows the setup sheet,
  /// signed in the compact bar. Notifies so the window resizes even while the
  /// panel is already visible (sign-in and sign-out both happen on-screen).
  void setPanelVariant(OverlayPanelVariant variant) {
    if (_panelVariant == variant) return;
    _panelVariant = variant;
    notifyListeners();
  }

  DesktopOnboardingStep _onboardingStep = DesktopOnboardingStep.welcome;
  DesktopOnboardingStep get onboardingStep => _onboardingStep;

  /// Reported by [DesktopOnboardingFlow] as the user steps through it.
  /// Notifies so [DesktopWindowService] can resize the setup sheet to that
  /// step's actual content height while the panel is already on screen.
  void setOnboardingStep(DesktopOnboardingStep step) {
    if (_onboardingStep == step) return;
    _onboardingStep = step;
    notifyListeners();
  }

  double? _measuredSetupHeight;
  double? get measuredSetupHeight => _measuredSetupHeight;

  /// Reported by `_SetupPanel` after every layout: the sheet's ACTUAL
  /// rendered height, measured directly rather than guessed. Per-step
  /// constants in DesktopWindowService are only the pre-measurement starting
  /// guess (avoids a visible "always opens oversized" flash); this is the
  /// authoritative value once available, and self-corrects on every content
  /// change (step, error text appearing, anything) with no new constant to
  /// tune. 1px-epsilon and clamped so float jitter and layout transients
  /// don't spam resizes.
  void reportMeasuredSetupHeight(double height) {
    final clamped = height.clamp(120.0, 560.0);
    if (_measuredSetupHeight != null &&
        (clamped - _measuredSetupHeight!).abs() < 1) {
      return;
    }
    _measuredSetupHeight = clamped;
    notifyListeners();
  }

  /// Global hotkey Ctrl+Alt+B: toggles. Hidden summons the panel; the panel
  /// dismisses (ending any live voice); the pill restores the panel. During
  /// pointing it is the escape hatch (a click-through window can't get Esc).
  void hotkeyPressed() {
    switch (_presentation) {
      case OverlayPresentation.hidden:
        _presentation = OverlayPresentation.panel;
      case OverlayPresentation.panel:
        _hideEndingVoice();
      case OverlayPresentation.pill:
        _presentation = OverlayPresentation.panel;
      case OverlayPresentation.pointing:
        onCancelPointing?.call();
        return; // the cancel path drives endPointing + its own notify
    }
    notifyListeners();
  }

  /// Tray click / second app instance / boot: always lands on the panel.
  void summon() {
    if (_presentation == OverlayPresentation.panel) return;
    if (_presentation == OverlayPresentation.pointing) {
      onCancelPointing?.call();
    }
    _presentation = OverlayPresentation.panel;
    notifyListeners();
  }

  /// Esc ends everything from anywhere.
  void escPressed() {
    if (_presentation == OverlayPresentation.pointing) {
      onCancelPointing?.call();
    }
    _hideEndingVoice();
    notifyListeners();
  }

  /// Window lost focus (user clicked another app). Pointing windows are
  /// click-through and lose focus by design, so they never react here.
  void focusLost() {
    if (_presentation != OverlayPresentation.panel) return;
    _presentation =
        _voiceActive ? OverlayPresentation.pill : OverlayPresentation.hidden;
    notifyListeners();
  }

  /// Click on the pill restores the full panel (conversation continues).
  void pillActivated() {
    if (_presentation != OverlayPresentation.pill) return;
    _presentation = OverlayPresentation.panel;
    notifyListeners();
  }

  /// The pointing service takes the window fullscreen click-through for a
  /// flight animation. Remembers where to come back to.
  void startPointing() {
    if (_presentation == OverlayPresentation.pointing) return;
    _beforePointing = _presentation;
    _presentation = OverlayPresentation.pointing;
    notifyListeners();
  }

  /// Pointing finished or was cancelled: restore the prior surface. A stale
  /// pill (voice ended mid-flight) collapses to hidden so the pill's
  /// mic-is-live meaning stays truthful.
  void endPointing() {
    if (_presentation != OverlayPresentation.pointing) return;
    var restored = _beforePointing;
    if (restored == OverlayPresentation.pill && !_voiceActive) {
      restored = OverlayPresentation.hidden;
    }
    _presentation = restored;
    notifyListeners();
  }

  /// Voice layer reports session state. Enforces the visibility invariant in
  /// both directions: a session starting while hidden forces the panel up, and
  /// a session ending while collapsed lets the pill vanish.
  void setVoiceActive(bool active) {
    if (_voiceActive == active) return;
    _voiceActive = active;
    if (active && _presentation == OverlayPresentation.hidden) {
      _presentation = OverlayPresentation.panel;
    }
    if (!active && _presentation == OverlayPresentation.pill) {
      _presentation = OverlayPresentation.hidden;
    }
    notifyListeners();
  }

  void _hideEndingVoice() {
    if (_voiceActive) {
      onEndVoiceSession?.call();
      _voiceActive = false;
    }
    _presentation = OverlayPresentation.hidden;
  }
}
