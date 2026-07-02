import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:provider/provider.dart';
import 'package:url_launcher/url_launcher.dart';

import '../../../core/theme/app_colors.dart';
import '../../../core/theme/app_theme.dart';
import '../../../core/utils/pairing_code_input_formatter.dart';
import '../../../data/models/voice_models.dart';
import '../../../data/services/desktop/overlay_controller.dart';
import '../../../data/services/desktop/pairing_service.dart';
import '../../../data/services/desktop/pointing_overlay_service.dart';
import '../../../data/services/desktop/screen_demo_service.dart';
import '../../../data/services/desktop/screen_sight_service.dart';
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

/// Root app for the desktop overlay window.
class DesktopOverlayApp extends StatefulWidget {
  const DesktopOverlayApp({super.key});

  @override
  State<DesktopOverlayApp> createState() => _DesktopOverlayAppState();
}

class _DesktopOverlayAppState extends State<DesktopOverlayApp> {
  @override
  void initState() {
    super.initState();
    // Deferred: initialize() notifies immediately (loading state), which is
    // unsafe while the first build is still mounting the provider tree.
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (mounted) context.read<AuthViewModel>().initialize();
    });
    // Esc is handled at the hardware-keyboard level, NOT via focus-scoped
    // shortcuts: after hide/show cycles the Flutter focus tree may hold no
    // focused node, and a focus-scoped shortcut silently stops firing
    // (observed in M1 testing).
    HardwareKeyboard.instance.addHandler(_handleKeyEvent);
  }

  @override
  void dispose() {
    HardwareKeyboard.instance.removeHandler(_handleKeyEvent);
    super.dispose();
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
      theme: AppTheme.dark,
      home: const OverlayPanel(),
    );
  }
}

/// The Spotlight-style overlay surface. Renders panel or pill depending on
/// [OverlayController.presentation]; the window itself is transparent, so this
/// widget paints the whole visible shape (near-opaque cream card, glass
/// border). No BackdropFilter: on a transparent window there is nothing behind
/// the app surface to blur.
class OverlayPanel extends StatelessWidget {
  const OverlayPanel({super.key});

  @override
  Widget build(BuildContext context) {
    final overlayController = context.watch<OverlayController>();

    return Scaffold(
      backgroundColor: Colors.transparent,
      body: switch (overlayController.presentation) {
        OverlayPresentation.pill => const _PillSurface(),
        OverlayPresentation.pointing => const _PointingSurface(),
        _ => const _PanelSurface(),
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

// Fully opaque surface: on a transparent window, Windows composites even
// slight surface alpha into heavy bleed-through of whatever is behind the
// overlay (observed in M1 testing). The rounded silhouette still comes from
// window transparency around the margins.
BoxDecoration _overlaySurfaceDecoration({required double radius}) {
  return BoxDecoration(
    color: AppColors.background,
    borderRadius: BorderRadius.circular(radius),
    border: Border.all(color: AppColors.glassBorderLight),
    boxShadow: const [
      BoxShadow(
        color: Color(0x333A3228),
        blurRadius: 28,
        offset: Offset(0, 8),
      ),
    ],
  );
}

class _PanelSurface extends StatelessWidget {
  const _PanelSurface();

  @override
  Widget build(BuildContext context) {
    final authViewModel = context.watch<AuthViewModel>();
    final voiceViewModel = context.watch<DesktopVoiceViewModel>();
    final screenSight = context.watch<ScreenSightService>();
    final signedIn = authViewModel.user != null;

    return Container(
      margin: const EdgeInsets.all(8),
      padding: const EdgeInsets.fromLTRB(20, 16, 20, 20),
      decoration: _overlaySurfaceDecoration(radius: 24),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Row(
            children: [
              VoiceSphere(
                  intensity: _sphereIntensityFor(voiceViewModel.status),
                  size: 40),
              const SizedBox(width: 12),
              Text('Buddy',
                  style: Theme.of(context).textTheme.titleLarge?.copyWith(
                        color: AppColors.textPrimary,
                        fontWeight: FontWeight.w600,
                      )),
              if (screenSight.armed) ...[
                const SizedBox(width: 10),
                const _ScreenSightIndicator(),
              ],
              const Spacer(),
              Text('Esc hides · Ctrl+Alt+B toggles',
                  style: Theme.of(context).textTheme.bodySmall?.copyWith(
                        color: AppColors.textPrimary.withValues(alpha: 0.45),
                      )),
              if (signedIn)
                IconButton(
                  tooltip: screenSight.armed
                      ? 'Stop letting Buddy see your screen'
                      : 'Let Buddy see your screen (Ctrl+Alt+S)',
                  iconSize: 16,
                  visualDensity: VisualDensity.compact,
                  color: screenSight.armed
                      ? AppColors.accentBase
                      : AppColors.textPrimary.withValues(alpha: 0.45),
                  icon: Icon(screenSight.armed
                      ? Icons.visibility_rounded
                      : Icons.visibility_off_rounded),
                  onPressed: () =>
                      context.read<ScreenSightService>().toggleArmed(),
                ),
              if (signedIn)
                IconButton(
                  tooltip: 'Sign out',
                  iconSize: 16,
                  visualDensity: VisualDensity.compact,
                  color: AppColors.textPrimary.withValues(alpha: 0.45),
                  icon: const Icon(Icons.logout_rounded),
                  onPressed: () => context.read<AuthViewModel>().signOut(),
                ),
            ],
          ),
          const SizedBox(height: 16),
          if (signedIn && context.watch<ScreenDemoService>().shouldOfferDemo)
            const _ScreenDemoInvite(),
          Expanded(
            child: signedIn
                ? const _VoicePanelBody()
                : const DesktopOnboardingFlow(linkStep: _DesktopSignInForm()),
          ),
        ],
      ),
    );
  }
}

/// First-run invitation to the screen-sight demo: one button press captures
/// once, Buddy points at something on the real screen with a playful comment,
/// and the card never returns. The press itself is the consent for that
/// single look.
class _ScreenDemoInvite extends StatelessWidget {
  const _ScreenDemoInvite();

  @override
  Widget build(BuildContext context) {
    final demoService = context.watch<ScreenDemoService>();
    return Container(
      margin: const EdgeInsets.only(bottom: 10),
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
      decoration: BoxDecoration(
        color: AppColors.accentBase.withValues(alpha: 0.08),
        borderRadius: BorderRadius.circular(14),
        border:
            Border.all(color: AppColors.accentBase.withValues(alpha: 0.25)),
      ),
      child: Row(
        children: [
          const Icon(Icons.visibility_rounded,
              size: 16, color: AppColors.accentBase),
          const SizedBox(width: 8),
          Expanded(
            child: Text(
              'Buddy can see your screen when you let him. Want to see?',
              style: Theme.of(context).textTheme.bodySmall?.copyWith(
                    color: AppColors.textPrimary.withValues(alpha: 0.8),
                  ),
            ),
          ),
          TextButton(
            onPressed: demoService.running
                ? null
                : () => context.read<ScreenDemoService>().dismiss(),
            child: const Text('Not now', style: TextStyle(fontSize: 12)),
          ),
          FilledButton(
            style: FilledButton.styleFrom(
              visualDensity: VisualDensity.compact,
              textStyle: const TextStyle(fontSize: 12),
            ),
            onPressed: demoService.running
                ? null
                : () => context.read<ScreenDemoService>().runDemo(),
            child: Text(demoService.running ? 'Looking...' : 'Show me'),
          ),
        ],
      ),
    );
  }
}

/// Live voice surface: status, captions, error state with retry.
class _VoicePanelBody extends StatelessWidget {
  const _VoicePanelBody();

  static const _statusLabels = {
    VoiceSessionStatus.connecting: 'Getting Buddy on the line...',
    VoiceSessionStatus.ready: 'Listening',
    VoiceSessionStatus.listening: 'Listening',
    VoiceSessionStatus.processing: 'Thinking...',
    VoiceSessionStatus.speaking: '',
  };

  @override
  Widget build(BuildContext context) {
    final voiceViewModel = context.watch<DesktopVoiceViewModel>();

    if (voiceViewModel.status == VoiceSessionStatus.error) {
      return _VoiceErrorBody(viewModel: voiceViewModel);
    }

    if (voiceViewModel.status == VoiceSessionStatus.disconnected ||
        voiceViewModel.status == VoiceSessionStatus.ended) {
      return Center(
        child: FilledButton.icon(
          onPressed: () =>
              context.read<DesktopVoiceViewModel>().startSession(),
          icon: const Icon(Icons.mic_rounded),
          label: const Text('Talk to Buddy'),
        ),
      );
    }

    final statusLabel = _statusLabels[voiceViewModel.status] ?? '';
    return Column(
      mainAxisAlignment: MainAxisAlignment.center,
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        if (voiceViewModel.userCaption.isNotEmpty)
          Text(
            voiceViewModel.userCaption,
            textAlign: TextAlign.center,
            maxLines: 2,
            overflow: TextOverflow.ellipsis,
            style: Theme.of(context).textTheme.bodyMedium?.copyWith(
                  color: AppColors.textPrimary.withValues(alpha: 0.5),
                  fontStyle: FontStyle.italic,
                ),
          ),
        const SizedBox(height: 8),
        if (voiceViewModel.assistantCaption.isNotEmpty)
          Flexible(
            child: SingleChildScrollView(
              reverse: true,
              child: Text(
                voiceViewModel.assistantCaption,
                textAlign: TextAlign.center,
                style: Theme.of(context).textTheme.titleMedium?.copyWith(
                      color: AppColors.textPrimary,
                      height: 1.35,
                    ),
              ),
            ),
          ),
        if (statusLabel.isNotEmpty) ...[
          const SizedBox(height: 10),
          Text(
            statusLabel,
            textAlign: TextAlign.center,
            style: Theme.of(context).textTheme.bodySmall?.copyWith(
                  color: AppColors.textPrimary.withValues(alpha: 0.45),
                ),
          ),
        ],
      ],
    );
  }
}

class _VoiceErrorBody extends StatelessWidget {
  const _VoiceErrorBody({required this.viewModel});

  final DesktopVoiceViewModel viewModel;

  @override
  Widget build(BuildContext context) {
    return Center(
      child: Column(
        mainAxisAlignment: MainAxisAlignment.center,
        children: [
          Text(
            viewModel.errorMessage ?? 'Something went sideways with the call.',
            textAlign: TextAlign.center,
            style: Theme.of(context).textTheme.bodyMedium?.copyWith(
                  color: AppColors.textPrimary.withValues(alpha: 0.75),
                ),
          ),
          const SizedBox(height: 12),
          Row(
            mainAxisAlignment: MainAxisAlignment.center,
            children: [
              FilledButton(
                onPressed: () =>
                    context.read<DesktopVoiceViewModel>().startSession(),
                child: const Text('Try again'),
              ),
              if (viewModel.showMicSettingsHint) ...[
                const SizedBox(width: 8),
                OutlinedButton(
                  onPressed: () => launchUrl(
                      Uri.parse('ms-settings:privacy-microphone')),
                  child: const Text('Open mic settings'),
                ),
              ],
            ],
          ),
        ],
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
                color: AppColors.textPrimary.withValues(alpha: 0.7),
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
              style: const TextStyle(color: Color(0xFFB3452E), fontSize: 12)),
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

/// The always-visible proof that Buddy can see the screen (the screen-sight
/// counterpart of the mic-live invariant): shown on the panel header and the
/// pill whenever sight is armed.
class _ScreenSightIndicator extends StatelessWidget {
  const _ScreenSightIndicator();

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
      decoration: BoxDecoration(
        color: AppColors.accentBase.withValues(alpha: 0.12),
        borderRadius: BorderRadius.circular(20),
      ),
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          const Icon(Icons.visibility_rounded,
              size: 12, color: AppColors.accentBase),
          const SizedBox(width: 4),
          Text('Seeing your screen',
              style: Theme.of(context).textTheme.bodySmall?.copyWith(
                    color: AppColors.accentBase,
                    fontSize: 11,
                  )),
        ],
      ),
    );
  }
}

/// Compact always-on-top pill shown when focus leaves mid-conversation: the
/// visible proof the mic is live (the invariant), with the latest caption.
/// Clicking restores the panel; the conversation never pauses.
class _PillSurface extends StatelessWidget {
  const _PillSurface();

  @override
  Widget build(BuildContext context) {
    final voiceViewModel = context.watch<DesktopVoiceViewModel>();
    final screenSight = context.watch<ScreenSightService>();
    final caption = voiceViewModel.assistantCaption.isNotEmpty
        ? voiceViewModel.assistantCaption
        : 'Buddy is listening...';

    return GestureDetector(
      onTap: () => context.read<OverlayController>().pillActivated(),
      child: Container(
        margin: const EdgeInsets.all(6),
        padding: const EdgeInsets.symmetric(horizontal: 14),
        decoration: _overlaySurfaceDecoration(radius: 30),
        child: Row(
          children: [
            VoiceSphere(
                intensity: _sphereIntensityFor(voiceViewModel.status),
                size: 32),
            const SizedBox(width: 10),
            Expanded(
              child: Text(caption,
                  overflow: TextOverflow.ellipsis,
                  style: Theme.of(context).textTheme.bodyMedium?.copyWith(
                        color: AppColors.textPrimary,
                      )),
            ),
            if (screenSight.armed) ...[
              const SizedBox(width: 8),
              const Icon(Icons.visibility_rounded,
                  size: 14, color: AppColors.accentBase),
            ],
          ],
        ),
      ),
    );
  }
}
