# Changelog

All notable production release changes are recorded here. The current release
version is `2.4.0+16`.

Every version bump in `pubspec.yaml` gets an entry here in the same commit,
including the exact text uploaded to the Play Console release notes field.

## 2.4.0+16 - 2026-08-28

The Buddy Keyboard release.

### Play Store release notes

```text
What's new

• Buddy Keyboard: a new keyboard for your phone. Buddy helps you write,
  rewrite, shorten, and reply to anyone without leaving the app you're in.
• Reminders arrive with clearer wording and better timing.
• Fixes and stability improvements.
```

### Added (mobile)

- **Buddy Keyboard**, an Android input method. Buddy drafts, rewrites, and
  replies inline in any app, with a writing-tools action row above the keys.

### Changed (mobile)

- Reminder copy is generated as structured text for clearer, better-timed
  notifications, with reminder tiers resolved at set time.
- Reminder retries and lifetime are now bounded, so a stuck reminder cannot
  repeat forever.
- Deep-link handling and billing customer identity when returning from web
  checkout.
- Keyboard action row spacing and initialism rendering in spoken text.

### Backend only, no mobile surface

These shipped in the same commit range but serve Aura-Desktop, not the phone.
They must never appear in Play Store notes.

- Interview Companion REST routes: brief, streamed answers, STT token,
  reflection (desktop-only, `ECOSYSTEM.md` 5a-2).
- Interview Mode job-description transfer over the voice worker
  (desktop-only, `ECOSYSTEM.md` 5a).
- Guide Supervisor for Guide Mode, which is armed natively on desktop
  (`ECOSYSTEM.md` 7b).
- Meetings service cleanup (desktop-only capture, `ECOSYSTEM.md` 5b).

### Play policy

- Removed `USE_FULL_SCREEN_INTENT` and its companion permissions. Build 15 was
  rejected over this. Alarms still ring on schedule; they no longer take over
  the lock screen.

## 2.4.0+15 - 2026-08-14 (rejected by Play, not shipped)

Rejected for `USE_FULL_SCREEN_INTENT`. Its contents reached users in build 16.

### Added

- Alarms. Buddy can set a device-local alarm that rings on schedule, with
  snooze and day-of-week repeats.
- A voice picker for choosing how Buddy sounds.

## 2.3.0+13 - 2026-07-27

### Added

- Added the Get Better library with 21 curated stories, a featured story, and
  related stories to explore.
- Added options to save stories, mark them complete, share them, and discuss
  them with Buddy.

### Changed

- Get Better stories now load from a stable, locally cached catalog for a
  faster and more reliable experience.
- Improved Android 15 edge-to-edge display handling.
- Reduced memory risk when selecting file attachments.
- Enabled optimized Android release shrinking for better app size and
  performance.

## Unreleased - Android billing cleanup

### Changed

- Removed obsolete R8/ProGuard rules for `com.android.billingclient` from
  `android/app/proguard-rules.pro`.
- The Android client does not declare or use Google Play Billing or Flutter
  in-app-purchase packages.

### Payment and entitlement behavior retained

- `PaywallScreen` presents subscription choices only when backend steering
  permits web checkout.
- `SubscriptionService.openCheckout` POSTs to the backend checkout endpoint
  and opens the returned Dodo checkout URL in the device's external browser.
- The app never grants an entitlement after a checkout attempt. The backend's
  Dodo webhook remains the entitlement writer; the app updates after the
  entitlement push or a resume-time entitlement refetch.

### Why this release is safe for Google Play

The obsolete ProGuard rules were the only Android-specific references to the
Google Play Billing namespace found in the repository manifests and source.
The source and lockfile do not declare a Google Billing or Flutter
in-app-purchase package. Upload a newly built AAB to every active Play track
so Play Console can re-scan the artifact.

The local Gradle dependency inspection could not run because this shell has no
`JAVA_HOME` or `java` configured. If Play Console still reports Billing after
the new upload, configure Java 17 and run:

```powershell
cd android
.\gradlew.bat :app:dependencyInsight --dependency com.android.billingclient --configuration releaseRuntimeClasspath
```

### Build and upload checklist

Run these commands in PowerShell from the repository root. Replace
`<NEXT_VERSION_CODE>` with an integer higher than every version code already
uploaded to Google Play. Do not reuse `11`.

```powershell
flutter pub get
flutter analyze
flutter test
flutter build appbundle --release --build-number=<NEXT_VERSION_CODE>
Get-FileHash .\build\app\outputs\bundle\release\app-release.aab -Algorithm SHA256
```

The resulting bundle is:

```text
build\app\outputs\bundle\release\app-release.aab
```

There is no configured repository command for publishing to Google Play.
Upload this AAB manually in Play Console:

1. Open **Play Console > Aura > Release > Production** (and each track you
   actively publish to).
2. Create or edit the release and upload `app-release.aab`.
3. Use the release notes below, review, and roll out the release.
4. Confirm Play Console has completed processing and that the Billing Library
   warning is cleared.

### Google Play release notes

```text
This update removes unused legacy Android billing configuration. Subscriptions
continue to be completed securely in your browser, with access syncing back to
the app automatically after payment.
```

### Validation and rollback

- Validate a signed release build with the commands above before upload.
- On a test account, open the paywall, start checkout, complete or cancel in
  the browser, return to the app, and verify that the displayed entitlement is
  refreshed from the backend.
- If a release issue is found after rollout, halt or roll back the Play release
  in Play Console. The removed rules have no source-level runtime behavior
  because the app does not import or call BillingClient.
