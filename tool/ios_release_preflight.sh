#!/usr/bin/env bash
#
# iOS release preflight for Aura. Run from the repo root before archiving:
#
#     bash tool/ios_release_preflight.sh
#
# Every check here exists because getting it wrong costs an App Store review
# cycle rather than a compile error. Checks are read-only; nothing is modified.
#
# This replaces tool/ios_release_preflight.ps1, which was written against a
# bundle ID and Firebase app that were reverted, asserted the opposite of the
# shipped configuration, and required a file that had been deleted. Delete the
# .ps1; it fails on its first assertion and cannot be right on a Mac anyway.

set -uo pipefail
cd "$(dirname "$0")/.."

BUNDLE_ID="com.varundevs.aura"
TEAM_ID="ZVG983V9JA"
PBXPROJ="ios/Runner.xcodeproj/project.pbxproj"
INFO="ios/Runner/Info.plist"

failures=0
pass() { printf '  ok    %s\n' "$1"; }
fail() { printf '  FAIL  %s\n' "$1"; failures=$((failures + 1)); }
section() { printf '\n%s\n' "$1"; }

check_contains() { # file needle label
  if grep -qF -- "$2" "$1" 2>/dev/null; then pass "$3"; else fail "$3"; fi
}

section "Identity"
check_contains "$PBXPROJ" "PRODUCT_BUNDLE_IDENTIFIER = $BUNDLE_ID;" "bundle ID is $BUNDLE_ID"
check_contains "$PBXPROJ" "DEVELOPMENT_TEAM = $TEAM_ID;" "development team is $TEAM_ID"
if [ -f ios/Runner/GoogleService-Info.plist ]; then
  gs_bundle=$(/usr/libexec/PlistBuddy -c "Print :BUNDLE_ID" ios/Runner/GoogleService-Info.plist 2>/dev/null)
  [ "$gs_bundle" = "$BUNDLE_ID" ] \
    && pass "GoogleService-Info.plist BUNDLE_ID matches" \
    || fail "GoogleService-Info.plist BUNDLE_ID is '$gs_bundle', expected $BUNDLE_ID"
  # The Google Sign-In callback scheme must be the reversed client ID, or sign-in
  # dead-ends after the browser hands control back.
  rev=$(/usr/libexec/PlistBuddy -c "Print :REVERSED_CLIENT_ID" ios/Runner/GoogleService-Info.plist 2>/dev/null)
  check_contains "$INFO" "$rev" "reversed client ID is registered as a URL scheme"
else
  fail "ios/Runner/GoogleService-Info.plist is missing (it is gitignored, so a fresh clone will not have it)"
fi

section "Signing"
if grep -q "iPhone Developer" "$PBXPROJ"; then
  fail "legacy CODE_SIGN_IDENTITY 'iPhone Developer' is set; a Release archive will not pick the distribution certificate"
else
  pass "no legacy 'iPhone Developer' signing identity"
fi
check_contains "$PBXPROJ" "CODE_SIGN_STYLE = Automatic;" "automatic signing is pinned"
if security find-identity -v -p codesigning 2>/dev/null | grep -q "Apple Distribution"; then
  pass "an Apple Distribution certificate exists in the keychain"
else
  fail "no Apple Distribution certificate in the keychain (a Developer ID certificate is macOS-only and cannot sign an iOS App Store build)"
fi

section "Capabilities"
check_contains ios/Runner/Runner.entitlements "production" "release entitlements request the production APNs environment"
check_contains ios/Runner/Runner.entitlements "com.apple.developer.applesignin" "Sign in with Apple is declared"

section "Store requirements"
[ -f ios/Runner/PrivacyInfo.xcprivacy ] \
  && pass "privacy manifest exists" \
  || fail "ios/Runner/PrivacyInfo.xcprivacy is missing (rejected after upload as ITMS-91053)"
check_contains "$PBXPROJ" "PrivacyInfo.xcprivacy in Resources" "privacy manifest is in Copy Bundle Resources (a manifest not in the bundle does nothing)"
enc=$(/usr/libexec/PlistBuddy -c "Print :ITSAppUsesNonExemptEncryption" "$INFO" 2>/dev/null)
[ "$enc" = "false" ] \
  && pass "export compliance answered in Info.plist" \
  || fail "ITSAppUsesNonExemptEncryption is '$enc'; every upload will ask the encryption question"
if grep -q "fetch" <(/usr/libexec/PlistBuddy -c "Print :UIBackgroundModes" "$INFO" 2>/dev/null); then
  fail "UIBackgroundModes declares 'fetch' but nothing uses background fetch"
else
  pass "UIBackgroundModes declares only what the app uses"
fi

section "Dependency hygiene"
[ -f ios/Podfile.lock ] \
  && pass "Podfile.lock exists" \
  || fail "ios/Podfile.lock is missing; pod resolution is not reproducible"
if grep -qi "GoogleAdsOnDeviceConversion\|GoogleAppMeasurement/IdentitySupport" ios/Podfile.lock 2>/dev/null; then
  fail "an IDFA/ad-attribution pod resolved; App Tracking Transparency would become mandatory"
else
  pass "no IDFA or ad-attribution pods resolved"
fi
# permission_handler compiles every handler unless told otherwise, and each one it
# links is a permission Apple then demands a purpose string for.
for macro in PERMISSION_CONTACTS PERMISSION_EVENTS PERMISSION_LOCATION PERMISSION_SPEECH_RECOGNIZER; do
  check_contains ios/Podfile "$macro" "$macro is switched off in the Podfile"
done
check_contains ios/Podfile "PERMISSION_MICROPHONE=1" "PERMISSION_MICROPHONE stays on (voice depends on it)"

section "Version"
printf '  pubspec version: %s\n' "$(grep -m1 '^version:' pubspec.yaml | awk '{print $2}')"
printf '  CHANGELOG says:  %s\n' "$(grep -m1 -o '`[0-9][^`]*`' CHANGELOG.md | tr -d '`')"
printf '  (a version bump needs a CHANGELOG entry in the same commit)\n'

section "Result"
if [ "$failures" -eq 0 ]; then
  printf '  all checks passed\n\n'
else
  printf '  %s check(s) failed\n\n' "$failures"
fi
exit "$failures"
