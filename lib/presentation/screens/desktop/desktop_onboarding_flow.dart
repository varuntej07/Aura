import 'package:flutter/material.dart';
import 'package:qr_flutter/qr_flutter.dart';
import 'package:shared_preferences/shared_preferences.dart';

import '../../../core/theme/app_colors.dart';

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
/// The panel window is a fixed 560x360, so every step lays out horizontally
/// compact. Once a user reaches the link step the flow is marked seen and
/// later signed-out launches land directly on the link step.
class DesktopOnboardingFlow extends StatefulWidget {
  const DesktopOnboardingFlow({super.key, required this.linkStep});

  /// The existing pairing/email sign-in form, injected so this flow owns
  /// only the guidance around it.
  final Widget linkStep;

  @override
  State<DesktopOnboardingFlow> createState() => _DesktopOnboardingFlowState();
}

class _DesktopOnboardingFlowState extends State<DesktopOnboardingFlow> {
  static const int _welcomeStep = 0;
  static const int _getAppStep = 1;
  static const int _linkStep = 2;
  static const int _stepCount = 3;

  int _step = _welcomeStep;
  bool _resolvedSeenFlag = false;

  @override
  void initState() {
    super.initState();
    SharedPreferences.getInstance().then((prefs) {
      if (!mounted) return;
      setState(() {
        if (prefs.getBool(desktopOnboardingSeenPreferenceKey) ?? false) {
          _step = _linkStep;
        }
        _resolvedSeenFlag = true;
      });
    });
  }

  void _goToStep(int step) {
    setState(() => _step = step);
    if (step == _linkStep) {
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
      children: [
        Expanded(
          child: switch (_step) {
            _welcomeStep => _WelcomeStep(
                onNext: () => _goToStep(_getAppStep),
                onSkipToLink: () => _goToStep(_linkStep),
              ),
            _getAppStep => _GetAppStep(
                onNext: () => _goToStep(_linkStep),
                onBack: () => _goToStep(_welcomeStep),
              ),
            _ => widget.linkStep,
          },
        ),
        const SizedBox(height: 8),
        Row(
          mainAxisAlignment: MainAxisAlignment.center,
          children: [
            _ProgressDots(current: _step, count: _stepCount),
            if (_step == _linkStep) ...[
              const SizedBox(width: 12),
              _SmallTextButton(
                label: 'New here?',
                onPressed: () => _goToStep(_welcomeStep),
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
                  color: AppColors.textPrimary,
                  fontWeight: FontWeight.w600,
                )),
        const SizedBox(height: 6),
        Text('Talk things through, stay on track, and pick up right where '
            'your phone left off.',
            textAlign: TextAlign.center,
            style: Theme.of(context).textTheme.bodySmall?.copyWith(
                  color: AppColors.textPrimary.withValues(alpha: 0.7),
                )),
        const SizedBox(height: 14),
        Row(
          mainAxisAlignment: MainAxisAlignment.center,
          children: const [
            _HotkeyChip(keys: 'Ctrl + Alt + B', action: 'summon'),
            SizedBox(width: 10),
            _HotkeyChip(keys: 'Esc', action: 'hide'),
          ],
        ),
        const SizedBox(height: 16),
        FilledButton(onPressed: onNext, child: const Text('Get set up')),
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
            color: Colors.white,
            borderRadius: BorderRadius.circular(12),
            border: Border.all(color: AppColors.glassBorderLight),
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
                        color: AppColors.textPrimary,
                        fontWeight: FontWeight.w600,
                      )),
              const SizedBox(height: 6),
              Text("Buddy's memory lives in your Aura account, and the phone "
                  'app is where it starts. Scan the code, or visit '
                  'auravoiceapp.com/app.',
                  style: Theme.of(context).textTheme.bodySmall?.copyWith(
                        color: AppColors.textPrimary.withValues(alpha: 0.7),
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

class _HotkeyChip extends StatelessWidget {
  const _HotkeyChip({required this.keys, required this.action});

  final String keys;
  final String action;

  @override
  Widget build(BuildContext context) {
    return Row(
      mainAxisSize: MainAxisSize.min,
      children: [
        Container(
          padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
          decoration: BoxDecoration(
            color: AppColors.glassWhiteFill,
            borderRadius: BorderRadius.circular(6),
            border: Border.all(color: AppColors.glassBorderLight),
          ),
          child: Text(keys,
              style: Theme.of(context).textTheme.bodySmall?.copyWith(
                    color: AppColors.textPrimary,
                    fontWeight: FontWeight.w600,
                  )),
        ),
        const SizedBox(width: 5),
        Text(action,
            style: Theme.of(context).textTheme.bodySmall?.copyWith(
                  color: AppColors.textPrimary.withValues(alpha: 0.55),
                )),
      ],
    );
  }
}

class _ProgressDots extends StatelessWidget {
  const _ProgressDots({required this.current, required this.count});

  final int current;
  final int count;

  @override
  Widget build(BuildContext context) {
    return Row(
      mainAxisSize: MainAxisSize.min,
      children: [
        for (var i = 0; i < count; i++)
          Container(
            width: 6,
            height: 6,
            margin: const EdgeInsets.symmetric(horizontal: 3),
            decoration: BoxDecoration(
              shape: BoxShape.circle,
              color: i == current
                  ? AppColors.accentBase
                  : AppColors.textPrimary.withValues(alpha: 0.2),
            ),
          ),
      ],
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
