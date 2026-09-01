$ErrorActionPreference = 'Stop'

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$bundleId = 'dev.varuntej.aura'
$legacyBundleId = 'com.' + 'varundevs.aura'
$googleAppId = '1:620715294422:ios:958693caa47842606e1377'
$googleClientId =
    '620715294422-lfnj0qk2rditl5neviusofbmv0c7o7r0.apps.googleusercontent.com'

function Assert-FileExists([string]$relativePath) {
    $path = Join-Path $repoRoot $relativePath
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Missing required file: $relativePath"
    }
}

function Assert-FileContains([string]$relativePath, [string]$text) {
    $path = Join-Path $repoRoot $relativePath
    $content = Get-Content -Raw -LiteralPath $path
    if (-not $content.Contains($text)) {
        throw "$relativePath does not contain required value: $text"
    }
}

$requiredFiles = @(
    'ios/Podfile',
    'ios/GoogleService-Info.plist',
    'ios/Runner/Info.plist',
    'ios/Runner/Runner.entitlements',
    'ios/Runner.xcodeproj/project.pbxproj',
    'lib/firebase_options.dart'
)
$requiredFiles | ForEach-Object { Assert-FileExists $_ }

$legacyMatches = & git -C $repoRoot grep -n -- $legacyBundleId 2>$null
if ($LASTEXITCODE -eq 0) {
    throw "Legacy bundle ID remains in tracked files:`n$($legacyMatches -join "`n")"
}
if ($LASTEXITCODE -gt 1) {
    throw 'git grep failed while checking the legacy bundle ID.'
}

Assert-FileContains 'ios/GoogleService-Info.plist' "<string>$bundleId</string>"
Assert-FileContains 'ios/GoogleService-Info.plist' "<string>$googleAppId</string>"
Assert-FileContains 'ios/Runner/Info.plist' "<string>$googleClientId</string>"
Assert-FileContains 'ios/Runner/Info.plist' '<key>ITSAppUsesNonExemptEncryption</key>'
Assert-FileContains 'ios/Runner/Runner.entitlements' `
    '<key>com.apple.developer.applesignin</key>'
Assert-FileContains 'ios/Runner.xcodeproj/project.pbxproj' `
    "PRODUCT_BUNDLE_IDENTIFIER = $bundleId;"
Assert-FileContains 'ios/Runner.xcodeproj/project.pbxproj' `
    'com.apple.SignInWithApple'
Assert-FileContains 'ios/Runner.xcodeproj/project.pbxproj' `
    'GoogleService-Info.plist in Resources'
Assert-FileContains 'lib/firebase_options.dart' "appId: '$googleAppId'"
Assert-FileContains 'lib/firebase_options.dart' "iosBundleId: '$bundleId'"
Assert-FileContains 'lib/data/services/firebase_auth_service.dart' `
    'Future<Result<User>> signInWithApple()'

Write-Host 'Windows iOS release preflight passed.' -ForegroundColor Green
Write-Host "Bundle ID: $bundleId"
Write-Host "Firebase Apple app: $googleAppId"
Write-Host ''
Write-Host 'Mac-only work remaining:' -ForegroundColor Yellow
Write-Host '1. Install current Xcode, Flutter, and CocoaPods.'
Write-Host '2. Run: flutter pub get'
Write-Host '3. Run: cd ios && pod install'
Write-Host '4. Open ios/Runner.xcworkspace, choose your Apple team, and let Xcode sign.'
Write-Host '5. Enable dev.varuntej.aura for Push Notifications and Sign in with Apple in the Apple portal.'
Write-Host '6. Enable Apple as a Firebase Authentication provider using the Apple team/key details.'
Write-Host '7. Test Apple/Google/email login, push, voice, photos, and account deletion on a real iPhone.'
Write-Host '8. Run: flutter build ipa --release'
