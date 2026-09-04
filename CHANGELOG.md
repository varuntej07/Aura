# Changelog

All notable production release changes are recorded here. The current release
version is `2.5.0+17`.

Every version bump in `pubspec.yaml` gets an entry here in the same commit,
including the exact text uploaded to the Play Console release notes field.

## 2.5.0+17 - 2026-09-03

The first iOS release. Nothing in this entry changes the Android app's
behaviour, so the Play notes below are deliberately thin: this version exists to
put Aura on the App Store.

### Play Store release notes

```text
What's new

• Stability and performance improvements under the hood.
```

### Added (iOS)

- **Aura is on the App Store.** iPhone only for this release; the iPad layout
  has not been exercised and the target now declares iPhone alone.
- **In-App Purchase.** iOS sells Companion and Pro through StoreKit, at the
  App Store's own localized price, with Restore Purchases and a link to Apple's
  subscription management sheet. `lib/data/services/store_purchase_service.dart`.
- Privacy manifest, a real launch image, and an App Store preflight script
  (`tool/ios_release_preflight.sh`).

### Changed

- **Payments are now split by platform, never by country.** iOS uses StoreKit
  because App Store Guideline 3.1.1 forbids sending users to an outside payment
  page for digital content; every other surface still uses Dodo web checkout.
  The iOS build shows no price constant, no checkout button, and no pointer to
  the website for buying. The rule lives in `SubscriptionService` so the
  checkout call itself is gated, not only the UI.
- One entitlement document, two sellers, told apart by its `source` field.
  Neither may cancel or expire a subscription the other owns, so a Dodo
  `subscription.expired` for an abandoned web plan can no longer drop a paying
  App Store subscriber to the free tier.
- Firebase Analytics now uses the ad-id-free variant. The default pulled
  ad-attribution and IDFA pods, which would have obliged an App Tracking
  Transparency prompt for something the product does not do.
- Only the microphone permission handler is compiled in. The rest were linking
  Contacts, EventKit, CoreLocation and Speech with no purpose strings.

### Backend only, no mobile surface

- `POST /billing/apple/transaction` and `POST /billing/apple/notifications`.
  Apple's signed payloads are verified against the Apple Root CA G3 certificate
  committed at `backend/src/resources/apple`, using Apple's own library rather
  than hand-rolled certificate-chain checking.

### Not yet verified

- No sandbox purchase has been made. The StoreKit path is written and compiles,
  and the backend's verification and entitlement logic were exercised directly,
  but no real transaction has gone through end to end. It cannot until the Paid
  Applications agreement is active and the four products exist in App Store
  Connect.

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
