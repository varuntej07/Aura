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
/// [beatPeriodMs] is optional timing metadata for the alarm ripples. Zero means
/// no beat has been declared for the clip, so the native alarm screen uses its
/// ambient ripple timing instead of inventing one.
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
      'assets/alarm_tones/${slug == kAlarmToneBuddy ? kAlarmToneBed : slug}.wav';

  bool get isBundled => slug != kAlarmToneBuddy;
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
const String kAlarmToneBed = 'morning-clock-alarm';

/// Order is the order the picker renders: familiar alarms first, followed by
/// the more distinctive character sounds.
const List<AlarmTone> kAlarmTones = [
  AlarmTone(
    slug: 'morning-clock-alarm',
    label: 'Morning Clock',
    blurb: 'A familiar bedside alarm for everyday wake-ups.',
    beatPeriodMs: 0,
    tint: Color(0xFFE89B5A), // warm
  ),
  AlarmTone(
    slug: 'alert-alarm',
    label: 'Alert',
    blurb: 'A straightforward alert-style alarm.',
    beatPeriodMs: 0,
    tint: Color(0xFF6EE1EB), // cyan
  ),
  AlarmTone(
    slug: 'buzzer-alarm',
    label: 'Alarm Buzzer',
    blurb: 'A classic buzzer-style alarm.',
    beatPeriodMs: 0,
    tint: Color(0xFF966EF5), // violet
  ),
  AlarmTone(
    slug: 'warning-buzzer',
    label: 'Warning Buzzer',
    blurb: 'A warning-style buzzer for urgent alarms.',
    beatPeriodMs: 0,
    tint: Color(0xFFF0B67A), // warm soft
  ),
  AlarmTone(
    slug: 'street-public-alarm',
    label: 'Public Alarm',
    blurb: 'A public alarm sound with a larger presence.',
    beatPeriodMs: 0,
    tint: Color(0xFF5A96FF), // blue
  ),
  AlarmTone(
    slug: 'battleship-alarm',
    label: 'Battleship',
    blurb: 'A naval-style alarm with a dramatic character.',
    beatPeriodMs: 0,
    tint: Color(0xFF34E3CB), // teal bright
  ),
  AlarmTone(
    slug: 'retro-game-emergency',
    label: 'Retro Emergency',
    blurb: 'An arcade-style emergency alarm.',
    beatPeriodMs: 0,
    tint: Color(0xFF966EF5), // violet
  ),
  AlarmTone(
    slug: 'rooster-crowing',
    label: 'Morning Rooster',
    blurb: 'A classic rooster call for the morning.',
    beatPeriodMs: 0,
    tint: Color(0xFFE89B5A), // warm
  ),
  AlarmTone(
    slug: 'short-rooster-crowing',
    label: 'Quick Rooster',
    blurb: 'A shorter rooster call that gets to the point.',
    beatPeriodMs: 0,
    tint: Color(0xFF6EE1EB), // cyan
  ),
  AlarmTone(
    slug: kAlarmToneBuddy,
    label: "Buddy's voice",
    blurb: 'Morning Clock, then Buddy reads your reminder out loud.',
    beatPeriodMs: 0,
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
