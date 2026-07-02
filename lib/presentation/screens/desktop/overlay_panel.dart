import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:provider/provider.dart';
import 'package:url_launcher/url_launcher.dart';

import '../../../core/theme/desktop_glass_theme.dart';
import '../../../core/utils/pairing_code_input_formatter.dart';
import '../../../data/models/voice_models.dart';
import '../../../data/services/desktop/overlay_controller.dart';
import '../../../data/services/desktop/pairing_service.dart';
import '../../../data/services/desktop/pointing_overlay_service.dart';
import '../../../data/services/desktop/screen_sight_service.dart';
import '../../../data/services/desktop/window_effects_service.dart';
import '../../viewmodels/auth_viewmodel.dart';
import '../../viewmodels/desktop_voice_viewmodel.dart';
import '../../widgets/desktop/pointer_buddy.dart';
import '../../widgets/voice_sphere.dart';
import 'desktop_onboarding_flow.dart';

double _sphereIntensityFor(VoiceSessionStatus status) {
  return switch (status) {
    VoiceSessionStatus.listening => 0.55,
    VoiceSessionStatus.processing => 0.75,
    VoiceSessionStatus.speaking => 0.95,
    VoiceSessionStatus.connecting => 0.45,
    _ => 0.35,
  };
}

/// True while a session is live in any form (the mic is or is about to be
/// hot). Drives the mic icon's active state and tap behavior.
bool _sessionLive(VoiceSessionStatus status) {
  return switch (status) {
    VoiceSessionStatus.connecting ||
    VoiceSessionStatus.ready ||
    VoiceSessionStatus.listening ||
    VoiceSessionStatus.processing ||
    VoiceSessionStatus.speaking =>
      true,
    _ => false,
  };
}

/// Root app for the desktop overlay window.
class DesktopOverlayApp extends StatefulWidget {
  const DesktopOverlayApp({super.key});

  @override
  State<DesktopOverlayApp> createState() => _DesktopOverlayAppState();
}

class _DesktopOverlayAppState extends State<DesktopOverlayApp> {
  AuthViewModel? _authViewModelForVariant;

  @override
  void initState() {
    super.initState();
    // Deferred: initialize() notifies immediately (loading state), which is
    // unsafe while the first build is still mounting the provider tree. The
    // variant listener rides the same deferral for the same reason: pushing a
    // variant into the provider-listened OverlayController mid-mount trips
    // the framework's !_dirty assertion.
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted) return;
      final auth = context.read<AuthViewModel>();
      auth.initialize();
      _authViewModelForVariant = auth..addListener(_syncPanelVariant);
      _syncPanelVariant();
    });
    // Esc is handled at the hardware-keyboard level, NOT via focus-scoped
    // shortcuts: after hide/show cycles the Flutter focus tree may hold no
    // focused node, and a focus-scoped shortcut silently stops firing
    // (observed in M1 testing).
    HardwareKeyboard.instance.addHandler(_handleKeyEvent);
  }

  @override
  void dispose() {
    _authViewModelForVariant?.removeListener(_syncPanelVariant);
    HardwareKeyboard.instance.removeHandler(_handleKeyEvent);
    super.dispose();
  }

  /// Auth state -> panel shape: signed out gets the tall setup sheet, signed
  /// in the compact bar. The window service resizes off this.
  void _syncPanelVariant() {
    if (!mounted) return;
    final signedIn = _authViewModelForVariant?.isAuthenticated ?? false;
    context.read<OverlayController>().setPanelVariant(
        signedIn ? OverlayPanelVariant.bar : OverlayPanelVariant.setup);
  }

  bool _handleKeyEvent(KeyEvent event) {
    if (event is KeyDownEvent &&
        event.logicalKey == LogicalKeyboardKey.escape) {
      context.read<OverlayController>().escPressed();
      return true;
    }
    return false;
  }

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'Buddy',
      debugShowCheckedModeBanner: false,
      theme: buildDesktopGlassTheme(),
      home: const OverlayPanel(),
    );
  }
}

/// The glass overlay surface. The window itself carries the real frost (DWM
/// acrylic, toggled by DesktopWindowService per presentation) and is sized
/// exactly to the content, so every surface here paints edge-to-edge; the
/// rounded silhouette comes from DWM corner rounding, matched by
/// [desktopGlassCornerRadius] on painted borders.
class OverlayPanel extends StatelessWidget {
  const OverlayPanel({super.key});

  @override
  Widget build(BuildContext context) {
    final overlayController = context.watch<OverlayController>();
    final signedIn = context.watch<AuthViewModel>().user != null;

    return Scaffold(
      backgroundColor: Colors.transparent,
      body: switch (overlayController.presentation) {
        OverlayPresentation.pill => const _GlassPill(),
        OverlayPresentation.pointing => const _PointingSurface(),
        _ => signedIn ? const _VoiceBar() : const _SetupPanel(),
      },
    );
  }
}

/// Fullscreen click-through surface during a pointer flight: nothing but the
/// buddy and its label over a fully transparent window.
class _PointingSurface extends StatelessWidget {
  const _PointingSurface();

  @override
  Widget build(BuildContext context) {
    final pointing = context.watch<PointingOverlayService>().active;
    if (pointing == null) return const SizedBox.shrink();
    return PointerBuddy(
      // Keyed so a replacement target restarts the flight from the top.
      key: ValueKey(pointing.targetInWindow),
      target: pointing.targetInWindow,
      label: pointing.label,
    );
  }
}

/// Shared glass sheet: content wash + hairline border over the window's
/// native acrylic. Falls back to a near-opaque surface when the OS acrylic
/// call is unavailable (translucency over an un-blurred desktop is
/// unreadable).
class _GlassSurface extends StatelessWidget {
  const _GlassSurface({required this.child});

  final Widget child;

  @override
  Widget build(BuildContext context) {
    final glassSupported =
        context.watch<WindowEffectsService>().glassSupported ?? true;
    return Container(
      decoration: BoxDecoration(
        color: glassSupported
            ? DesktopGlassColors.surfaceWash
            : DesktopGlassColors.surfaceFallback,
        borderRadius: BorderRadius.circular(desktopGlassCornerRadius),
        border: Border.all(color: DesktopGlassColors.border),
      ),
      child: child,
    );
  }
}

/// Compact icon action on the glass bar; [active] glows teal.
class _BarIconButton extends StatelessWidget {
  const _BarIconButton({
    required this.icon,
    required this.tooltip,
    required this.onPressed,
    this.active = false,
  });

  final IconData icon;
  final String tooltip;
  final VoidCallback onPressed;
  final bool active;

  @override
  Widget build(BuildContext context) {
    return IconButton(
      tooltip: tooltip,
      iconSize: 18,
      visualDensity: VisualDensity.compact,
      color:
          active ? DesktopGlassColors.accent : DesktopGlassColors.iconIdle,
      icon: Icon(icon),
      onPressed: onPressed,
    );
  }
}

/// The signed-in surface: one glass bar. Sphere, a single line of Buddy's
/// latest words, and icon toggles — screen sight and voice — plus sign-out.
/// No titles, no status labels, no user-speech captions (design decision:
/// the sphere and the audio ARE the status).
class _VoiceBar extends StatelessWidget {
  const _VoiceBar();

  @override
  Widget build(BuildContext context) {
    final voiceViewModel = context.watch<DesktopVoiceViewModel>();
    final screenSight = context.watch<ScreenSightService>();
    final hasError = voiceViewModel.status == VoiceSessionStatus.error;
    final live = _sessionLive(voiceViewModel.status);

    final caption = hasError
        ? (voiceViewModel.errorMessage ??
            'Something went sideways with the call.')
        : voiceViewModel.assistantCaption;

    return _GlassSurface(
      child: Padding(
        padding: const EdgeInsets.symmetric(horizontal: 14),
        child: Row(
          children: [
            VoiceSphere(
                intensity: _sphereIntensityFor(voiceViewModel.status),
                size: 34),
            const SizedBox(width: 12),
            Expanded(
              child: Text(
                caption,
                maxLines: 1,
                overflow: TextOverflow.ellipsis,
                style: Theme.of(context).textTheme.bodyMedium?.copyWith(
                      color: hasError
                          ? DesktopGlassColors.danger
                          : DesktopGlassColors.textBright,
                    ),
              ),
            ),
            const SizedBox(width: 8),
            if (hasError && voiceViewModel.showMicSettingsHint)
              _BarIconButton(
                icon: Icons.settings_rounded,
                tooltip: 'Open mic settings',
                onPressed: () =>
                    launchUrl(Uri.parse('ms-settings:privacy-microphone')),
              ),
            _BarIconButton(
              icon: screenSight.armed
                  ? Icons.visibility_rounded
                  : Icons.visibility_off_rounded,
              tooltip: screenSight.armed
                  ? 'Stop letting Buddy see your screen'
                  : 'Let Buddy see your screen (Ctrl+Alt+S)',
              active: screenSight.armed,
              onPressed: () =>
                  context.read<ScreenSightService>().toggleArmed(),
            ),
            _BarIconButton(
              icon: hasError
                  ? Icons.refresh_rounded
                  : (live ? Icons.mic_rounded : Icons.mic_none_rounded),
              tooltip: hasError
                  ? 'Try again'
                  : (live ? 'End the conversation' : 'Talk to Buddy'),
              active: live && !hasError,
              onPressed: () {
                final viewModel = context.read<DesktopVoiceViewModel>();
                if (live) {
                  viewModel.endSession();
                } else {
                  viewModel.startSession();
                }
              },
            ),
            _BarIconButton(
              icon: Icons.logout_rounded,
              tooltip: 'Sign out',
              onPressed: () => context.read<AuthViewModel>().signOut(),
            ),
          ],
        ),
      ),
    );
  }
}

/// Signed-out surface: the tall glass sheet holding first-run onboarding and
/// the pairing/sign-in form (forms need words; the no-text rule applies to
/// the talking surface, not setup).
class _SetupPanel extends StatelessWidget {
  const _SetupPanel();

  @override
  Widget build(BuildContext context) {
    return _GlassSurface(
      child: Padding(
        padding: const EdgeInsets.fromLTRB(20, 16, 20, 20),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Row(
              children: [
                const VoiceSphere(intensity: 0.35, size: 32),
                const SizedBox(width: 10),
                Text('Buddy',
                    style: Theme.of(context).textTheme.titleLarge?.copyWith(
                          fontSize: 20,
                        )),
              ],
            ),
            const SizedBox(height: 12),
            const Expanded(
              child: DesktopOnboardingFlow(linkStep: _DesktopSignInForm()),
            ),
          ],
        ),
      ),
    );
  }
}

/// Compact always-on-top pill shown when focus leaves mid-conversation: the
/// visible proof the mic is live (the invariant), with the latest caption.
/// The teal eye marks screen sight while armed (same invariant, for sight).
/// Clicking restores the bar; the conversation never pauses.
class _GlassPill extends StatelessWidget {
  const _GlassPill();

  @override
  Widget build(BuildContext context) {
    final voiceViewModel = context.watch<DesktopVoiceViewModel>();
    final screenSight = context.watch<ScreenSightService>();
    final caption = voiceViewModel.assistantCaption.isNotEmpty
        ? voiceViewModel.assistantCaption
        : 'Buddy is listening...';

    return GestureDetector(
      onTap: () => context.read<OverlayController>().pillActivated(),
      child: _GlassSurface(
        child: Padding(
          padding: const EdgeInsets.symmetric(horizontal: 12),
          child: Row(
            children: [
              VoiceSphere(
                  intensity: _sphereIntensityFor(voiceViewModel.status),
                  size: 28),
              const SizedBox(width: 10),
              Expanded(
                child: Text(caption,
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                    style: Theme.of(context).textTheme.bodyMedium?.copyWith(
                          color: DesktopGlassColors.textDim,
                        )),
              ),
              if (screenSight.armed) ...[
                const SizedBox(width: 8),
                const Icon(Icons.visibility_rounded,
                    size: 14, color: DesktopGlassColors.accent),
              ],
            ],
          ),
        ),
      ),
    );
  }
}

class _DesktopSignInForm extends StatefulWidget {
  const _DesktopSignInForm();

  @override
  State<_DesktopSignInForm> createState() => _DesktopSignInFormState();
}

class _DesktopSignInFormState extends State<_DesktopSignInForm> {
  final _emailController = TextEditingController();
  final _passwordController = TextEditingController();
  final _codeController = TextEditingController();

  // Pairing is the primary path (review decision 2): Google-only accounts have
  // no password on Windows, but everyone has their phone.
  bool _emailMode = false;

  // Local submit tracking: the ViewModel's global loading state also covers
  // boot-time auth resolution (which can be slow on the Windows auth SDK), and
  // tying the button to it left users staring at a disabled "Signing in..."
  // before ever clicking anything (observed in M1 testing).
  bool _submitting = false;
  String? _pairingError;

  @override
  void dispose() {
    _emailController.dispose();
    _passwordController.dispose();
    _codeController.dispose();
    super.dispose();
  }

  Future<void> _submitEmail() async {
    setState(() => _submitting = true);
    try {
      await context.read<AuthViewModel>().signInWithEmail(
            _emailController.text.trim(),
            _passwordController.text,
          );
    } finally {
      if (mounted) setState(() => _submitting = false);
    }
  }

  Future<void> _submitPairingCode() async {
    setState(() {
      _submitting = true;
      _pairingError = null;
    });
    final result = await context
        .read<PairingService>()
        .claimAndSignIn(_codeController.text);
    if (!mounted) return;
    result.when(
      success: (_) {},
      failure: (error) => _pairingError = error.message,
    );
    setState(() => _submitting = false);
  }

  @override
  Widget build(BuildContext context) {
    final authViewModel = context.watch<AuthViewModel>();
    final errorMessage =
        _emailMode ? authViewModel.error?.message : _pairingError;

    return Column(
      mainAxisAlignment: MainAxisAlignment.center,
      children: [
        Text(
          _emailMode
              ? 'Sign in to bring Buddy to your desktop'
              : 'On your phone: Aura → Settings → Link this PC',
          style: Theme.of(context).textTheme.bodyMedium?.copyWith(
                color: DesktopGlassColors.textDim,
              ),
        ),
        const SizedBox(height: 12),
        if (_emailMode) ...[
          TextField(
            controller: _emailController,
            decoration:
                const InputDecoration(labelText: 'Email', isDense: true),
            keyboardType: TextInputType.emailAddress,
          ),
          TextField(
            controller: _passwordController,
            decoration:
                const InputDecoration(labelText: 'Password', isDense: true),
            obscureText: true,
            onSubmitted: (_) => _submitEmail(),
          ),
        ] else
          TextField(
            controller: _codeController,
            autofocus: true,
            textAlign: TextAlign.center,
            // The formatter uppercases and hyphenates as you type, so the code
            // is entered exactly as it reads on the phone with no Shift, Caps
            // Lock, or hyphen key. Linking starts by itself on the 8th
            // character; onSubmitted stays for Enter after an edit.
            inputFormatters: const [PairingCodeInputFormatter()],
            onChanged: (text) {
              if (!_submitting &&
                  PairingCodeInputFormatter.rawCode(text).length ==
                      PairingCodeInputFormatter.codeLength) {
                _submitPairingCode();
              }
            },
            style: const TextStyle(
              fontFamily: 'GeistMono',
              fontSize: 22,
              letterSpacing: 4,
              color: DesktopGlassColors.textBright,
            ),
            decoration: const InputDecoration(
              hintText: 'XXXX-XXXX',
              isDense: true,
            ),
            onSubmitted: (_) => _submitPairingCode(),
          ),
        const SizedBox(height: 12),
        if (errorMessage != null) ...[
          Text(errorMessage,
              textAlign: TextAlign.center,
              style: const TextStyle(
                  color: DesktopGlassColors.danger, fontSize: 12)),
          const SizedBox(height: 8),
        ],
        FilledButton(
          onPressed: _submitting
              ? null
              : (_emailMode ? _submitEmail : _submitPairingCode),
          child: Text(
            _submitting
                ? (_emailMode ? 'Signing in...' : 'Linking...')
                : (_emailMode ? 'Sign in' : 'Link this PC'),
          ),
        ),
        TextButton(
          onPressed: _submitting
              ? null
              : () => setState(() => _emailMode = !_emailMode),
          child: Text(
            _emailMode
                ? 'Link with your phone instead'
                : 'Use email & password instead',
            style: const TextStyle(fontSize: 12),
          ),
        ),
      ],
    );
  }
}
