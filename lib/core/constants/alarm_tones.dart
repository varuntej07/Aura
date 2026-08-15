/// The sounds an alarm can ring with, as the picker presents them.
///
/// Mirrors `android/app/src/main/kotlin/dev/varuntej/aura/alarm/AlarmTones.kt`
/// and `backend/src/services/alarm_tones.py`. Kotlin is the only one that
/// actually makes noise; the backend owns validation on the write path; this
/// list is presentation plus the asset path the preview player uses.
///
/// Slugs are the contract between the three. Keep them identical, and deploy
/// the backend before shipping an app build that offers a new one, or the
/// server drops the unknown slug and the alarm quietly falls back.
///
/// [beatPeriodMs] is not decoration. Every bundled clip is a seamless loop of an
/// exact integer number of these, and the alarm screen emits one ripple per
/// beat from a single anchor timestamp. A wrong value here does not break audio,
/// it desynchronises the water from the sound.
library;

import 'package:flutter/material.dart';

class AlarmTone {
  final String slug;
  final String label;

  /// One line about how it feels to be woken by it, not what synthesis made it.
  final String blurb;

  /// Milliseconds between beats, and so between ripples. Zero means the sound
  /// has no beat this build can know about, and the screen falls back to a slow
  /// ambient emission.
  final int beatPeriodMs;

  /// The card's identity colour, drawn from the ripple palette so the picker
  /// previews the water the tone will be ringing over.
  final Color tint;

  const AlarmTone({
    required this.slug,
    required this.label,
    required this.blurb,
    required this.beatPeriodMs,
    required this.tint,
  });

  /// Also the path Kotlin opens, prefixed with `flutter_assets/`. One file, so
  /// the preview and the 3 AM alarm can never be different sounds.
  String get previewAsset =>
      'assets/alarm_tones/${slug == kAlarmToneBuddy ? kAlarmToneBed : slug}.ogg';

  bool get isBundled => beatPeriodMs > 0 && slug != kAlarmToneBuddy;
}

/// Buddy speaks the reminder aloud in the user's chosen voice, over [_buddyBed].
///
/// Not a toggle sitting beside the tones: it IS one of the tones, so there is no
/// gate to be off for anyone.
const String kAlarmToneBuddy = 'buddy';

/// The user picked a sound from their own device with the system ringtone
/// picker. The URI lives in SharedPreferences on the device, never on a server:
/// it is a path into their own storage and means nothing to anyone else.
const String kAlarmToneDevice = 'device';

/// Empty preference: whatever the phone's own default alarm sound is. What every
/// account is on today, and the final fallback for every failure below it.
const String kAlarmToneSystemDefault = '';

/// The tone Buddy's voice rings over, and what an unresolvable slug becomes.
const String kAlarmToneBed = 'ripple';

/// Order is the order the picker renders: gentlest first, hardest to sleep
/// through last, because that is how someone shops for an alarm.
const List<AlarmTone> kAlarmTones = [
  AlarmTone(
    slug: 'ripple',
    label: 'Ripple',
    blurb: 'Soft wooden droplets. Wakes you without startling you.',
    beatPeriodMs: 750,
    tint: Color(0xFF6EE1EB), // cyan
  ),
  AlarmTone(
    slug: 'dawn',
    label: 'Dawn',
    blurb: 'A warm bell that swells and climbs. Slow on purpose.',
    beatPeriodMs: 1500,
    tint: Color(0xFFE89B5A), // warm
  ),
  AlarmTone(
    slug: 'tide',
    label: 'Tide',
    blurb: 'A low wash with a shimmer over it. The calmest one here.',
    beatPeriodMs: 2000,
    tint: Color(0xFF5A96FF), // blue
  ),
  AlarmTone(
    slug: 'chime',
    label: 'Chime',
    blurb: 'Bright glass bells, clear enough to cut through a dream.',
    beatPeriodMs: 900,
    tint: Color(0xFF34E3CB), // teal bright
  ),
  AlarmTone(
    slug: 'pulse',
    label: 'Pulse',
    blurb: 'Two firm notes, over and over. Hard to ignore.',
    beatPeriodMs: 600,
    tint: Color(0xFF966EF5), // violet
  ),
  AlarmTone(
    slug: 'ascent',
    label: 'Ascent',
    blurb: 'Climbs and gets louder every pass. For heavy sleepers.',
    beatPeriodMs: 500,
    tint: Color(0xFFF0B67A), // warm soft
  ),
  AlarmTone(
    slug: kAlarmToneBuddy,
    label: "Buddy's voice",
    blurb: 'Ripple, then Buddy reads your reminder out loud.',
    beatPeriodMs: 750,
    tint: Color(0xFF1EC8B0), // house teal
  ),
];

/// The tone for a slug, or null when this build does not ship it — an unknown
/// slug means the backend is ahead of the app, and the picker must not pretend
/// it can preview a clip it does not have.
AlarmTone? alarmToneFor(String? slug) {
  for (final tone in kAlarmTones) {
    if (tone.slug == slug) return tone;
  }
  return null;
}

/// What the picker shows as selected. Both special slugs and anything
/// unrecognised render as the system-default row rather than as nothing.
String displayAlarmToneSlug(String? stored) {
  final slug = (stored ?? '').trim();
  if (slug == kAlarmToneDevice) return kAlarmToneDevice;
  return alarmToneFor(slug) != null ? slug : kAlarmToneSystemDefault;
}
