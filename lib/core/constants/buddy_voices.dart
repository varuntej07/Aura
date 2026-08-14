/// The voices Buddy can speak in, as the picker presents them.
///
/// This mirrors `backend/src/agent/voice/voice_catalog.py`. The backend owns
/// resolution and entitlement — it is the only thing that decides which voice
/// actually reaches Cartesia — so this list is presentation only: label, blurb,
/// preview clip, and whether to draw a lock.
///
/// Slugs are the contract between the two. Keep them identical, and deploy the
/// backend before shipping an app build that offers a new one, or the worker
/// will reject the unknown slug and fall back to the default.
///
/// Preview clips live at `assets/voices/<slug>.mp3` and are regenerated with
/// `cd backend && python -m src.agent.generate_voice_previews`. Each voice speaks
/// its own sentence; the lines live in that script, and deliberately are not
/// mirrored here, because nothing renders them as text and a copy would drift.
library;

import 'package:flutter/material.dart';

class BuddyVoice {
  final String slug;
  final String label;

  /// One line, in Buddy's register rather than a voice-industry spec sheet.
  /// A user picking a companion cares what it feels like, not its age bracket.
  final String blurb;

  final bool paidOnly;

  /// The card's identity colour. Muted on purpose: eight of these sit together
  /// on the cream background, and saturated hues turn the picker into a paint
  /// chart. Also tints the whole screen while this voice is previewing.
  final Color tint;

  const BuddyVoice({
    required this.slug,
    required this.label,
    required this.blurb,
    required this.paidOnly,
    required this.tint,
  });

  String get previewAsset => 'assets/voices/$slug.mp3';
}

/// Order is the order the picker renders. Free voices first so a free user sees
/// what they can have before what they cannot.
///
/// Tints are ordered so that no two neighbours in the two-column grid land in
/// the same part of the wheel: the grid pairs them (katie, dallas), (tessa,
/// kira), (layla, jolene), (kyle, archie).
const List<BuddyVoice> kBuddyVoices = [
  BuddyVoice(
    slug: 'katie',
    label: 'Katie',
    blurb: 'Bright and clear. The voice Buddy has always had.',
    paidOnly: false,
    tint: Color(0xFFD98A7A), // soft coral
  ),
  BuddyVoice(
    slug: 'dallas',
    label: 'Dallas',
    blurb: 'Easy and grounded, like a friend on a long call.',
    paidOnly: false,
    tint: Color(0xFF7FA98A), // sage
  ),
  BuddyVoice(
    slug: 'tessa',
    label: 'Tessa',
    blurb: 'Warm and close. Sounds glad you called.',
    paidOnly: true,
    tint: Color(0xFFE0A97E), // peach
  ),
  BuddyVoice(
    slug: 'kira',
    label: 'Kira',
    blurb: 'Soft and steady. Leans in when things get heavy.',
    paidOnly: true,
    tint: Color(0xFF8391C4), // periwinkle
  ),
  BuddyVoice(
    slug: 'layla',
    label: 'Layla',
    blurb: 'Cool and unhurried. Never in a rush.',
    paidOnly: true,
    tint: Color(0xFF4FB3A5), // house teal
  ),
  BuddyVoice(
    slug: 'jolene',
    label: 'Jolene',
    blurb: 'Honeyed and Southern. Slow warmth.',
    paidOnly: true,
    tint: Color(0xFFC98F3F), // honey
  ),
  BuddyVoice(
    slug: 'kyle',
    label: 'Kyle',
    blurb: 'Open and easy, quick to laugh.',
    paidOnly: true,
    tint: Color(0xFF6FA8C9), // sky
  ),
  BuddyVoice(
    slug: 'archie',
    label: 'Archie',
    blurb: 'British, warm and a little dry.',
    paidOnly: true,
    tint: Color(0xFFB5715A), // clay
  ),
];

/// Matches `voice_catalog.DEFAULT_VOICE_SLUG`. Every existing account is on this
/// voice today, so it is what an empty stored preference resolves to.
const String kDefaultBuddyVoiceSlug = 'katie';

/// The stored slug, or the default when unset or unrecognised. A slug this build
/// does not know about means the backend is ahead of the app; showing the default
/// is honest, because the picker cannot preview a clip it did not ship.
BuddyVoice buddyVoiceFor(String? slug) {
  for (final voice in kBuddyVoices) {
    if (voice.slug == slug) return voice;
  }
  return kBuddyVoices.firstWhere((v) => v.slug == kDefaultBuddyVoiceSlug);
}
