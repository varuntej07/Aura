import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';
import 'package:just_audio/just_audio.dart';
import 'package:provider/provider.dart';

import '../../../core/constants/buddy_voices.dart';
import '../../../core/logging/app_logger.dart';
import '../../../core/theme/app_colors.dart';
import '../../../core/theme/glass_card.dart';
import '../../viewmodels/auth_viewmodel.dart';
import '../../viewmodels/settings_viewmodel.dart';
import '../../viewmodels/subscription_viewmodel.dart';
import '../../widgets/voice_waveform.dart';

/// Lets the user choose how Buddy sounds.
///
/// Previews are bundled MP3s, not live synthesis: tapping a voice must be
/// instant and free, and must work on a plane. They are rendered at the same
/// speed a real call uses, so what is auditioned is what is delivered. Each
/// voice speaks a different sentence, so comparing two of them feels like
/// meeting two people rather than auditing one TTS engine.
///
/// Every voice carries its own tint (see [BuddyVoice.tint]): it colours the
/// card and, while that clip plays, the whole screen.
class VoicePickerScreen extends StatefulWidget {
  const VoicePickerScreen({super.key});

  @override
  State<VoicePickerScreen> createState() => _VoicePickerScreenState();
}

class _VoicePickerScreenState extends State<VoicePickerScreen> {
  final AudioPlayer _player = AudioPlayer();
  String? _playingSlug;

  /// Guards the one shared [_player] against overlapping taps.
  ///
  /// Each preview claims the next number and re-checks it after every await,
  /// abandoning quietly the moment a later tap takes over. Without this, two
  /// taps in quick succession call setAsset concurrently on the same player and
  /// which clip is loaded when play() fires comes down to load timing — so the
  /// voice you hear is not the card you pressed, and auditioning the grid at
  /// speed replays whichever clip loaded first.
  int _previewRequest = 0;

  /// Survives the clip ending, so the ambient tint fades out from the colour it
  /// was rather than snapping back to teal the moment the voice stops.
  Color _ambientTint = kBuddyVoices.first.tint;

  @override
  void dispose() {
    _player.dispose();
    super.dispose();
  }

  Future<void> _preview(BuddyVoice voice) async {
    // Claimed before the first await, so any preview already in flight sees
    // itself superseded and stops touching the player.
    final request = ++_previewRequest;

    // Tapping the voice that is already talking stops it. Restarting the same
    // clip is never what that tap means.
    if (_playingSlug == voice.slug) {
      await _player.stop();
      if (mounted && request == _previewRequest) {
        setState(() => _playingSlug = null);
      }
      return;
    }

    // Otherwise restart rather than queue: tapping a second voice while the
    // first is still talking should switch immediately, which is how someone
    // actually compares two voices.
    setState(() {
      _playingSlug = voice.slug;
      _ambientTint = voice.tint;
    });
    try {
      await _player.stop();
      if (request != _previewRequest) return;
      await _player.setAsset(voice.previewAsset);
      if (request != _previewRequest) return;
      await _player.play();
    } catch (e, st) {
      // A missing or corrupt asset must not look like a voice that simply has
      // nothing to say. Surface it.
      AppLogger.error(
        'Voice preview failed for ${voice.slug}',
        error: e,
        stackTrace: st,
        tag: 'VoicePicker',
      );
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          const SnackBar(content: Text("Couldn't play that preview.")),
        );
      }
    } finally {
      // A superseded preview owns nothing any more: clearing here would wipe
      // the tint and play state the newer tap has already set.
      if (mounted &&
          request == _previewRequest &&
          _playingSlug == voice.slug) {
        setState(() => _playingSlug = null);
      }
    }
  }

  Future<void> _choose(BuddyVoice voice, bool unlocked) async {
    if (!unlocked) {
      context.push('/paywall');
      return;
    }
    await context.read<SettingsViewModel>().selectVoice(voice.slug);
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(
        content: Text("Got it. I'll sound like ${voice.label} from our next call."),
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final settingsVm = context.watch<SettingsViewModel>();
    final subscriptionVm = context.watch<SubscriptionViewModel>();
    final signedIn = context.watch<AuthViewModel>().user != null;

    // Trial accounts resolve to 'pro' server-side, so hasFeatureAccess is the
    // getter that matches what the worker will actually allow.
    final hasPaidAccess = subscriptionVm.hasFeatureAccess;
    final selectedSlug = settingsVm.settings?.ttsVoiceId ?? '';
    final selected = buddyVoiceFor(
      selectedSlug.isEmpty ? kDefaultBuddyVoiceSlug : selectedSlug,
    );

    return Scaffold(
      backgroundColor: Colors.transparent,
      body: AmbientBackground(
        child: Stack(
          children: [
            _AmbientVoiceTint(tint: _ambientTint, visible: _playingSlug != null),
            SafeArea(
              child: Column(
                children: [
                  Padding(
                    padding:
                        const EdgeInsets.symmetric(horizontal: 20, vertical: 12),
                    child: Row(
                      children: [
                        GlassIconButton(
                          icon: Icons.arrow_back_ios_new,
                          onTap: () => Navigator.pop(context),
                          iconSize: 17,
                        ),
                        const SizedBox(width: 14),
                        const Text(
                          "Buddy's voice",
                          style: TextStyle(
                            color: AppColors.textPrimary,
                            fontSize: 20,
                            fontWeight: FontWeight.w700,
                            letterSpacing: -0.5,
                          ),
                        ),
                      ],
                    ),
                  ),
                  Expanded(
                    child: CustomScrollView(
                      slivers: [
                        const SliverPadding(
                          padding: EdgeInsets.fromLTRB(20, 4, 20, 16),
                          sliver: SliverToBoxAdapter(
                            child: Text(
                              'Tap play to hear one. Tap the card to keep it. '
                              'Your pick takes effect on the next call.',
                              style: TextStyle(
                                color: AppColors.textTertiary,
                                fontSize: 13,
                                height: 1.4,
                              ),
                            ),
                          ),
                        ),
                        SliverPadding(
                          padding: const EdgeInsets.symmetric(horizontal: 16),
                          sliver: SliverGrid(
                            gridDelegate:
                                const SliverGridDelegateWithFixedCrossAxisCount(
                              crossAxisCount: 2,
                              mainAxisSpacing: 12,
                              crossAxisSpacing: 12,
                              childAspectRatio: 0.86,
                            ),
                            delegate: SliverChildBuilderDelegate(
                              (context, index) {
                                final voice = kBuddyVoices[index];
                                final unlocked =
                                    !voice.paidOnly || hasPaidAccess;
                                return _VoiceCard(
                                  voice: voice,
                                  selected:
                                      signedIn && voice.slug == selected.slug,
                                  unlocked: unlocked,
                                  playing: _playingSlug == voice.slug,
                                  onPreview: () => _preview(voice),
                                  onChoose: signedIn
                                      ? () => _choose(voice, unlocked)
                                      : null,
                                );
                              },
                              childCount: kBuddyVoices.length,
                            ),
                          ),
                        ),
                        SliverPadding(
                          padding: const EdgeInsets.fromLTRB(20, 16, 20, 32),
                          sliver: SliverToBoxAdapter(
                            child: signedIn
                                ? const SizedBox.shrink()
                                : const Text(
                                    'Sign in to keep a voice.',
                                    style: TextStyle(
                                      color: AppColors.textTertiary,
                                      fontSize: 13,
                                    ),
                                  ),
                          ),
                        ),
                      ],
                    ),
                  ),
                ],
              ),
            ),
          ],
        ),
      ),
    );
  }
}

/// Washes the screen in the playing voice's colour.
///
/// Deliberately mirrors [AmbientBackground]'s orb positions and sizes so this
/// reads as the existing background changing hue, not as a second set of blobs
/// arriving on top of the teal ones. [AmbientBackground] itself is shared by
/// every screen and stays neutral.
class _AmbientVoiceTint extends StatelessWidget {
  final Color tint;
  final bool visible;

  const _AmbientVoiceTint({required this.tint, required this.visible});

  @override
  Widget build(BuildContext context) {
    return IgnorePointer(
      child: AnimatedOpacity(
        opacity: visible ? 1 : 0,
        duration: const Duration(milliseconds: 450),
        curve: Curves.easeOut,
        child: TweenAnimationBuilder<Color?>(
          tween: ColorTween(end: tint),
          duration: const Duration(milliseconds: 450),
          curve: Curves.easeOut,
          builder: (context, color, _) {
            final resolved = color ?? tint;
            return Stack(
              children: [
                Positioned(
                  top: -80,
                  left: -80,
                  child: _orb(resolved.withValues(alpha: 0.16), 360),
                ),
                Positioned(
                  bottom: -100,
                  right: -60,
                  child: _orb(resolved.withValues(alpha: 0.10), 300),
                ),
              ],
            );
          },
        ),
      ),
    );
  }

  Widget _orb(Color color, double size) => Container(
        width: size,
        height: size,
        decoration: BoxDecoration(
          shape: BoxShape.circle,
          gradient: RadialGradient(colors: [color, Colors.transparent]),
        ),
      );
}

class _VoiceCard extends StatelessWidget {
  final BuddyVoice voice;
  final bool selected;
  final bool unlocked;
  final bool playing;
  final VoidCallback onPreview;
  final VoidCallback? onChoose;

  const _VoiceCard({
    required this.voice,
    required this.selected,
    required this.unlocked,
    required this.playing,
    required this.onPreview,
    required this.onChoose,
  });

  @override
  Widget build(BuildContext context) {
    final tint = voice.tint;
    // Fills are light so the cream shows through; the icon needs a darkened
    // version of the same hue to stay legible on it.
    final inkTint = Color.lerp(tint, Colors.black, 0.32)!;

    return GestureDetector(
      onTap: onChoose,
      child: FauxGlassCard(
        borderRadius: 20,
        padding: const EdgeInsets.all(14),
        borderColor: tint.withValues(alpha: selected ? 0.65 : 0.28),
        gradient: LinearGradient(
          begin: Alignment.topLeft,
          end: Alignment.bottomRight,
          colors: [
            tint.withValues(alpha: 0.16),
            tint.withValues(alpha: 0.05),
          ],
        ),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                // Preview stays tappable when locked: hearing a voice you cannot
                // have yet is the whole reason to upgrade, so muting the demo
                // would be self-defeating.
                GestureDetector(
                  onTap: onPreview,
                  child: Container(
                    width: 40,
                    height: 40,
                    decoration: BoxDecoration(
                      color: tint.withValues(alpha: playing ? 0.34 : 0.18),
                      shape: BoxShape.circle,
                    ),
                    child: Icon(
                      playing ? Icons.stop_rounded : Icons.play_arrow_rounded,
                      size: 22,
                      color: inkTint,
                    ),
                  ),
                ),
                const Spacer(),
                if (selected)
                  Icon(Icons.check_circle_rounded, size: 20, color: inkTint)
                else if (!unlocked)
                  const Icon(
                    Icons.lock_rounded,
                    size: 16,
                    color: AppColors.textTertiary,
                  ),
              ],
            ),
            // The wave owns the middle of the card rather than sitting as a
            // strip at the bottom: it is the thing that moves, so it gets the
            // space the play button and the name would otherwise leave empty.
            Expanded(
              child: Padding(
                padding: const EdgeInsets.symmetric(vertical: 8),
                child: VoiceWaveform(color: tint, active: playing),
              ),
            ),
            Opacity(
              opacity: unlocked ? 1 : 0.8,
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    voice.label,
                    style: const TextStyle(
                      color: AppColors.textPrimary,
                      fontSize: 16,
                      fontWeight: FontWeight.w600,
                    ),
                  ),
                  const SizedBox(height: 3),
                  Text(
                    voice.blurb,
                    maxLines: 2,
                    overflow: TextOverflow.ellipsis,
                    style: const TextStyle(
                      color: AppColors.textTertiary,
                      fontSize: 12.5,
                      height: 1.3,
                    ),
                  ),
                ],
              ),
            ),
          ],
        ),
      ),
    );
  }
}
