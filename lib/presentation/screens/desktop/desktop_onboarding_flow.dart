import 'package:flutter/material.dart';
import 'package:qr_flutter/qr_flutter.dart';
import 'package:shared_preferences/shared_preferences.dart';

import '../../../core/theme/app_colors.dart';
import '../../../core/theme/desktop_glass_theme.dart';
import '../../../data/services/desktop/overlay_controller.dart'
    show DesktopOnboardingStep;

/// Stable URL baked into shipped builds. It is a web-side redirect
/// (auravoiceapp.com/app), so where it lands can change after release
/// (waitlist today, the store listing later) without touching this binary.
const String getAuraAppUrl = 'https://auravoiceapp.com/app';

const String desktopOnboardingSeenPreferenceKey = 'desktop_onboarding_seen';

/// Signed-out first-run guidance for the desktop overlay. A fresh install
/// used to drop strangers straight onto the pairing-code screen with no
/// explanation that a phone app exists; this walks them there instead:
/// meet Buddy -> get the phone app (QR) -> link this PC.
///
/// The window resizes to each step's actual content height (via
/// [onStepChanged], read by [DesktopWindowService]) rather than one fixed
/// sheet tall enough for the tallest step. Once a user reaches the link step
/// the flow is marked seen and later signed-out launches land directly on it.
class DesktopOnboardingFlow extends StatefulWidget {
  const DesktopOnboardingFlow({
    super.key,
    required this.linkStep,
    this.onStepChanged,
  });

  /// The existing pairing/email sign-in form, injected so this flow owns
  /// only the guidance around it.
  final Widget linkStep;

  /// Fired whenever the visible step changes, including the initial resolve
  /// of the "seen onboarding" flag. Plain callback (not a Provider read) so
  /// this widget stays testable without a provider tree.
  final ValueChanged<DesktopOnboardingStep>? onStepChanged;

  @override
  State<DesktopOnboardingFlow> createState() => _DesktopOnboardingFlowState();
}

class _DesktopOnboardingFlowState extends State<DesktopOnboardingFlow> {
  DesktopOnboardingStep _step = DesktopOnboardingStep.welcome;
  bool _resolvedSeenFlag = false;

  @override
  void initState() {
    super.initState();
    SharedPreferences.getInstance().then((prefs) {
      if (!mounted) return;
      setState(() {
        if (prefs.getBool(desktopOnboardingSeenPreferenceKey) ?? false) {
          _step = DesktopOnboardingStep.link;
        }
        _resolvedSeenFlag = true;
      });
      widget.onStepChanged?.call(_step);
    });
  }

  void _goToStep(DesktopOnboardingStep step) {
    setState(() => _step = step);
    widget.onStepChanged?.call(step);
    if (step == DesktopOnboardingStep.link) {
      SharedPreferences.getInstance().then(
          (prefs) => prefs.setBool(desktopOnboardingSeenPreferenceKey, true));
    }
  }

  @override
  Widget build(BuildContext context) {
    // The seen-flag read is near-instant (cached singleton); rendering nothing
    // for that frame beats flashing the welcome step at returning users.
    if (!_resolvedSeenFlag) return const SizedBox.shrink();

    return Column(
      mainAxisSize: MainAxisSize.min,
      children: [
        switch (_step) {
          DesktopOnboardingStep.welcome => _WelcomeStep(
              onNext: () => _goToStep(DesktopOnboardingStep.getApp),
              onSkipToLink: () => _goToStep(DesktopOnboardingStep.link),
            ),
          DesktopOnboardingStep.getApp => _GetAppStep(
              onNext: () => _goToStep(DesktopOnboardingStep.link),
              onBack: () => _goToStep(DesktopOnboardingStep.welcome),
            ),
          DesktopOnboardingStep.link => widget.linkStep,
        },
        const SizedBox(height: 8),
        Row(
          mainAxisSize: MainAxisSize.min,
          children: [
            _ProgressDots(
              current: _step,
              onSelect: _goToStep,
            ),
            if (_step == DesktopOnboardingStep.link) ...[
              const SizedBox(width: 12),
              _SmallTextButton(
                label: 'New here?',
                onPressed: () => _goToStep(DesktopOnboardingStep.welcome),
              ),
            ],
          ],
        ),
      ],
    );
  }
}

class _WelcomeStep extends StatelessWidget {
  const _WelcomeStep({required this.onNext, required this.onSkipToLink});

  final VoidCallback onNext;
  final VoidCallback onSkipToLink;

  @override
  Widget build(BuildContext context) {
    return Column(
      mainAxisAlignment: MainAxisAlignment.center,
      children: [
        Text('Meet Buddy, your AI friend on this PC.',
            textAlign: TextAlign.center,
            style: Theme.of(context).textTheme.titleMedium?.copyWith(
                  color: DesktopGlassColors.textBright,
                  fontWeight: FontWeight.w600,
                )),
        const SizedBox(height: 6),
        Text('Talk things through, stay on track, and pick up right where '
            'your phone left off.',
            textAlign: TextAlign.center,
            style: Theme.of(context).textTheme.bodySmall?.copyWith(
                  color: DesktopGlassColors.textDim,
                )),
        const SizedBox(height: 10),
        // Windows 11 hides new tray icons in the overflow area by default, so
        // this is said explicitly once here rather than left to be discovered.
        Text(
            'Buddy lives in your system tray — press Ctrl+Alt+B anytime to '
            'bring it back.',
            textAlign: TextAlign.center,
            style: Theme.of(context).textTheme.bodySmall?.copyWith(
                  color: DesktopGlassColors.textDim,
                  fontSize: 11,
                )),
        const SizedBox(height: 16),
        FilledButton(onPressed: onNext, child: const Text('Get set up')),
        const SizedBox(height: 10),
        _SmallTextButton(
            label: 'Already have Aura? Link now', onPressed: onSkipToLink),
      ],
    );
  }
}

class _GetAppStep extends StatelessWidget {
  const _GetAppStep({required this.onNext, required this.onBack});

  final VoidCallback onNext;
  final VoidCallback onBack;

  @override
  Widget build(BuildContext context) {
    return Row(
      mainAxisAlignment: MainAxisAlignment.center,
      children: [
        Container(
          padding: const EdgeInsets.all(8),
          decoration: BoxDecoration(
            // The QR card stays white with dark-ink modules: scanners want
            // contrast, and a bright card reads as "this is the thing to
            // point your phone at" on the dark glass.
            color: Colors.white,
            borderRadius: BorderRadius.circular(20),
            border: Border.all(color: DesktopGlassColors.border),
          ),
          child: QrImageView(
            data: getAuraAppUrl,
            version: QrVersions.auto,
            size: 116,
            padding: EdgeInsets.zero,
            eyeStyle: const QrEyeStyle(
              eyeShape: QrEyeShape.square,
              color: AppColors.textPrimary,
            ),
            dataModuleStyle: const QrDataModuleStyle(
              dataModuleShape: QrDataModuleShape.square,
              color: AppColors.textPrimary,
            ),
          ),
        ),
        const SizedBox(width: 20),
        Flexible(
          child: Column(
            mainAxisAlignment: MainAxisAlignment.center,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text('First, grab Aura on your phone',
                  style: Theme.of(context).textTheme.titleMedium?.copyWith(
                        color: DesktopGlassColors.textBright,
                        fontWeight: FontWeight.w600,
                      )),
              const SizedBox(height: 6),
              Text("Buddy's memory lives in your Aura account, and the phone "
                  'app is where it starts. Scan the code, or visit '
                  'auravoiceapp.com/app.',
                  style: Theme.of(context).textTheme.bodySmall?.copyWith(
                        color: DesktopGlassColors.textDim,
                      )),
              const SizedBox(height: 12),
              Row(
                children: [
                  FilledButton(
                      onPressed: onNext, child: const Text('I have the app')),
                  const SizedBox(width: 4),
                  _SmallTextButton(label: 'Back', onPressed: onBack),
                ],
              ),
            ],
          ),
        ),
      ],
    );
  }
}

/// Step dots double as navigation: each dot jumps to its step (they read as
/// tappable, so they are). Housed in a faint glass pill so the nav control
/// itself reads as one small piece of glass, not three bare dots floating on
/// the surface.
class _ProgressDots extends StatelessWidget {
  const _ProgressDots({
    required this.current,
    required this.onSelect,
  });

  final DesktopOnboardingStep current;
  final void Function(DesktopOnboardingStep step) onSelect;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 3),
      decoration: BoxDecoration(
        color: DesktopGlassColors.fieldFill,
        borderRadius: BorderRadius.circular(12),
        border: Border.all(color: DesktopGlassColors.border),
      ),
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          for (final step in DesktopOnboardingStep.values)
            MouseRegion(
              cursor: SystemMouseCursors.click,
              child: GestureDetector(
                behavior: HitTestBehavior.opaque,
                onTap: () => onSelect(step),
                // Padding, not margin: the 6px dot alone is a hostile click
                // target; the padded box is the real hit area.
                child: Padding(
                  padding:
                      const EdgeInsets.symmetric(horizontal: 4, vertical: 6),
                  child: Container(
                    width: 6,
                    height: 6,
                    decoration: BoxDecoration(
                      shape: BoxShape.circle,
                      color: step == current
                          ? DesktopGlassColors.accent
                          : DesktopGlassColors.iconIdle,
                    ),
                  ),
                ),
              ),
            ),
        ],
      ),
    );
  }
}

class _SmallTextButton extends StatelessWidget {
  const _SmallTextButton({required this.label, required this.onPressed});

  final String label;
  final VoidCallback onPressed;

  @override
  Widget build(BuildContext context) {
    return TextButton(
      onPressed: onPressed,
      style: TextButton.styleFrom(
        padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
        minimumSize: Size.zero,
        tapTargetSize: MaterialTapTargetSize.shrinkWrap,
      ),
      child: Text(label, style: const TextStyle(fontSize: 12)),
    );
  }
}
