import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:provider/provider.dart';
import 'package:url_launcher/url_launcher.dart';
import 'package:window_manager/window_manager.dart';

import '../../../core/theme/desktop_glass_theme.dart';
import '../../../core/utils/pairing_code_input_formatter.dart';
import '../../../data/models/voice_models.dart';
import '../../../data/services/desktop/overlay_controller.dart';
import '../../../data/services/desktop/pairing_service.dart';
import '../../../data/services/desktop/pointing_overlay_service.dart';
import '../../../data/services/desktop/screen_sight_service.dart';
import '../../viewmodels/auth_viewmodel.dart';
import '../../viewmodels/desktop_voice_viewmodel.dart';
import '../../widgets/desktop/hotkey_hint.dart';
import '../../widgets/desktop/pointer_buddy.dart';
import 'desktop_onboarding_flow.dart';

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

/// The ENTIRE visible glass card. The native window behind this is always
/// fully transparent (windows/runner/window_effects_channel.cpp) — no OS
/// blur, no OS rounding — so this gradient fill, at real opacity, plus the
/// hairline border below IS the whole "glass" look, the one and only shape
/// authority (2026-07-10; see desktopGlassCornerRadius doc comment for why
/// native rounding was dropped). There's no separate "OS blur unavailable"
/// fallback anymore because there's no OS blur to ever be available or not.
class _GlassSurface extends StatelessWidget {
  const _GlassSurface({required this.child});

  final Widget child;

  @override
  Widget build(BuildContext context) {
    return CustomPaint(
      foregroundPainter: _GlassEdgePainter(),
      child: Container(
        decoration: BoxDecoration(
          borderRadius: BorderRadius.circular(desktopGlassCornerRadius),
          gradient: const LinearGradient(
            begin: Alignment.topCenter,
            end: Alignment.bottomCenter,
            colors: [
              DesktopGlassColors.sheenFaint,
              DesktopGlassColors.surfaceFill,
            ],
            stops: [0.0, 0.25],
          ),
        ),
        // The whole card is draggable (2026-07-10, per user request — the
        // overlay's position used to be fixed top-center of the cursor's
        // display). _WindowDragArea below.
        child: _WindowDragArea(child: child),
      ),
    );
  }
}

/// Like window_manager's own `DragToMoveArea`, minus its double-tap-to-
/// maximize behavior — this borderless HUD overlay should never fill the
/// screen from an accidental double-click. Translucent hit-testing lets taps
/// on buttons/fields underneath still resolve normally; only an actual drag
/// gesture starts a native window move (`windowManager.startDragging()`).
class _WindowDragArea extends StatelessWidget {
  const _WindowDragArea({required this.child});

  final Widget child;

  @override
  Widget build(BuildContext context) {
    return GestureDetector(
      behavior: HitTestBehavior.translucent,
      onPanStart: (_) => windowManager.startDragging(),
      child: child,
    );
  }
}

/// Hairline border with a vertical gradient stroke — bright where light
/// would catch the top edge of real glass, fading down the sides. Painted
/// because a [Border] cannot hold a gradient.
class _GlassEdgePainter extends CustomPainter {
  @override
  void paint(Canvas canvas, Size size) {
    final rect = Offset.zero & size;
    final paint = Paint()
      ..style = PaintingStyle.stroke
      ..strokeWidth = 1
      ..shader = const LinearGradient(
        begin: Alignment.topCenter,
        end: Alignment.bottomCenter,
        colors: [
          DesktopGlassColors.borderTop,
          DesktopGlassColors.borderBottom,
        ],
      ).createShader(rect);
    canvas.drawRRect(
      RRect.fromRectAndRadius(
        rect.deflate(0.5),
        const Radius.circular(desktopGlassCornerRadius),
      ),
      paint,
    );
  }

  @override
  bool shouldRepaint(covariant _GlassEdgePainter oldDelegate) => false;
}

/// Compact icon action on the glass bar. Idle icons sit at a high, crisp
/// opacity (not a grey smudge); [active] lifts to the light accent teal with
/// a soft colored glow, hover gets a subtle glass halo.
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
      style: IconButton.styleFrom(
        shape: const CircleBorder(),
        hoverColor: const Color(0x1AFFFFFF),
        highlightColor: const Color(0x26FFFFFF),
      ),
      icon: Icon(
        icon,
        color:
            active ? DesktopGlassColors.accent : DesktopGlassColors.iconIdle,
        shadows: active
            ? [
                Shadow(
                  color: DesktopGlassColors.accentGlow,
                  blurRadius: 10,
                ),
              ]
            : null,
      ),
      onPressed: onPressed,
    );
  }
}

/// Thin vertical hairline separating icon groups on the glass bar.
class _BarDivider extends StatelessWidget {
  const _BarDivider();

  @override
  Widget build(BuildContext context) {
    return Container(
      width: 1,
      height: 18,
      margin: const EdgeInsets.symmetric(horizontal: 6),
      color: DesktopGlassColors.border,
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
        padding: const EdgeInsets.symmetric(horizontal: 16),
        child: Row(
          children: [
            Image.asset('assets/icons/Aura-Icon.png', width: 34, height: 34),
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
            const _BarDivider(),
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
///
/// The window height is driven by ACTUAL measurement, not a guessed constant
/// (two rounds of hand-picked per-step heights both still left visible slack,
/// 2026-07-10): [Align] gives the inner [Padding]+[Column] loose constraints
/// so it sizes to its true natural content height even though the Scaffold
/// above always hands this widget the window's current (tight) height; that
/// natural size is measured every frame and reported to
/// [OverlayController.reportMeasuredSetupHeight], which
/// [DesktopWindowService] resizes the real window to. The outer
/// [_GlassSurface] still paints its background/border edge-to-edge at the
/// window's full current size regardless — only the CONTENT'S positioning
/// changes (top-aligned, natural size, no stretch).
class _SetupPanel extends StatefulWidget {
  const _SetupPanel();

  @override
  State<_SetupPanel> createState() => _SetupPanelState();
}

class _SetupPanelState extends State<_SetupPanel> {
  final _contentKey = GlobalKey();

  void _scheduleMeasurement() {
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted) return;
      final height = _contentKey.currentContext?.size?.height;
      if (height != null) {
        context.read<OverlayController>().reportMeasuredSetupHeight(height);
      }
    });
  }

  @override
  Widget build(BuildContext context) {
    _scheduleMeasurement();
    return _GlassSurface(
      child: Align(
        alignment: Alignment.topCenter,
        child: Padding(
          key: _contentKey,
          padding: const EdgeInsets.fromLTRB(24, 18, 24, 12),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              Row(
                children: [
                  Image.asset('assets/icons/Aura-Icon.png',
                      width: 32, height: 32),
                  const SizedBox(width: 12),
                  Text('Buddy',
                      style:
                          Theme.of(context).textTheme.titleLarge?.copyWith(
                                fontSize: 20,
                              )),
                  const Spacer(),
                  const HotkeyHint(
                    keys: ['Ctrl', 'Alt', 'B'],
                    action: 'summon Buddy anywhere',
                  ),
                  const SizedBox(width: 12),
                  const HotkeyHint(keys: ['Esc'], action: 'hide'),
                ],
              ),
              const SizedBox(height: 12),
              DesktopOnboardingFlow(
                linkStep: const _DesktopSignInForm(),
                onStepChanged: (step) =>
                    context.read<OverlayController>().setOnboardingStep(step),
              ),
            ],
          ),
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
              Image.asset('assets/icons/Aura-Icon.png', width: 28, height: 28),
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
                Icon(
                  Icons.visibility_rounded,
                  size: 14,
                  color: DesktopGlassColors.accent,
                  shadows: [
                    Shadow(
                      color: DesktopGlassColors.accentGlow,
                      blurRadius: 8,
                    ),
                  ],
                ),
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
              : 'On your phone: Aura -> Settings -> Link this PC',
          style: Theme.of(context).textTheme.bodyMedium?.copyWith(
                color: DesktopGlassColors.textDim,
              ),
        ),
        const SizedBox(height: 14),
        if (_emailMode) ...[
          TextField(
            controller: _emailController,
            style: const TextStyle(
              fontSize: 14,
              color: DesktopGlassColors.textBright,
            ),
            decoration: const InputDecoration(
              hintText: 'you@email.com',
              isDense: true,
              prefixIcon: Icon(Icons.mail_outline_rounded, size: 16),
              prefixIconConstraints:
                  BoxConstraints(minWidth: 36, minHeight: 36),
            ),
            keyboardType: TextInputType.emailAddress,
          ),
          const SizedBox(height: 10),
          TextField(
            controller: _passwordController,
            style: const TextStyle(
              fontSize: 14,
              color: DesktopGlassColors.textBright,
            ),
            decoration: const InputDecoration(
              hintText: 'Password',
              isDense: true,
              prefixIcon: Icon(Icons.lock_outline_rounded, size: 16),
              prefixIconConstraints:
                  BoxConstraints(minWidth: 36, minHeight: 36),
            ),
            obscureText: true,
            onSubmitted: (_) => _submitEmail(),
          ),
        ] else
          SizedBox(
            width: 220,
            child: TextField(
              controller: _codeController,
              autofocus: true,
              textAlign: TextAlign.center,
              // The formatter uppercases and hyphenates as you type, so the
              // code is entered exactly as it reads on the phone with no
              // Shift, Caps Lock, or hyphen key. Linking starts by itself on
              // the 8th character; onSubmitted stays for Enter after an edit.
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
          ),
        const SizedBox(height: 12),
        if (errorMessage != null) ...[
          Text(errorMessage,
              textAlign: TextAlign.center,
              style: const TextStyle(
                  color: DesktopGlassColors.danger, fontSize: 12)),
          const SizedBox(height: 8),
        ],
        Row(
          mainAxisAlignment: MainAxisAlignment.center,
          children: [
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
            const SizedBox(width: 8),
            TextButton(
              onPressed: _submitting
                  ? null
                  : () => setState(() => _emailMode = !_emailMode),
              style: TextButton.styleFrom(
                padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
                minimumSize: Size.zero,
                tapTargetSize: MaterialTapTargetSize.shrinkWrap,
              ),
              child: Text(
                _emailMode
                    ? 'Link with your phone instead'
                    : 'Use email & password instead',
                style: const TextStyle(fontSize: 12),
              ),
            ),
          ],
        ),
        const SizedBox(height: 6),
        const _LegalLinksRow(),
      ],
    );
  }
}

/// Footer links shown under the desktop sign-in/pairing form. This is the
/// only legal disclosure surface in the desktop app itself (the website
/// covers it for anyone who arrives via auravoiceapp.com, but the .exe can
/// also reach someone secondhand who never saw that page).
class _LegalLinksRow extends StatelessWidget {
  const _LegalLinksRow();

  @override
  Widget build(BuildContext context) {
    return Row(
      mainAxisAlignment: MainAxisAlignment.center,
      children: [
        _LegalLink(label: 'Privacy', url: 'https://auravoiceapp.com/privacy'),
        const Text(' · ',
            style:
                TextStyle(color: DesktopGlassColors.textDim, fontSize: 11)),
        _LegalLink(label: 'Terms', url: 'https://auravoiceapp.com/terms'),
      ],
    );
  }
}

class _LegalLink extends StatelessWidget {
  const _LegalLink({required this.label, required this.url});

  final String label;
  final String url;

  @override
  Widget build(BuildContext context) {
    return TextButton(
      onPressed: () => launchUrl(Uri.parse(url)),
      style: TextButton.styleFrom(
        padding: const EdgeInsets.symmetric(horizontal: 4, vertical: 4),
        minimumSize: Size.zero,
        tapTargetSize: MaterialTapTargetSize.shrinkWrap,
      ),
      child: Text(
        label,
        style: const TextStyle(
          fontSize: 11,
          color: DesktopGlassColors.textDim,
          decoration: TextDecoration.underline,
        ),
      ),
    );
  }
}
