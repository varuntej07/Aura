param(
    [string] $BenchmarkImeApk = "",
    [string] $TraceProcessor = "",
    [ValidateSet("Baseline", "Final")] [string] $Mode = "Final",
    [string] $OutputDirectory = ".\ime-benchmark-results",
    [string] $Python = "python",
    [switch] $SkipBuild
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$benchmarkImePackage = "dev.varuntej.aura.imebenchmarktarget"
$productionAuraPackage = "dev.varuntej.aura"
$benchmarkHostPackage = "dev.varuntej.aura.imebenchmark"
$benchmarkImeComponent =
    "$benchmarkImePackage/dev.varuntej.aura.keyboard.BuddyImeService"

function Invoke-Adb {
    # A basic function intentionally consumes $args. Declaring PowerShell parameters here makes
    # valid adb flags such as -o and -e collide with abbreviated common parameters.
    $Arguments = @($args)
    # adb writes successful transfer progress to stderr. With the script-wide Stop policy,
    # PowerShell otherwise turns messages such as "1 file pushed" into terminating errors.
    $previousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        $output = & adb @Arguments 2>&1
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    if ($exitCode -ne 0) { throw "adb $Arguments failed:`n$output" }
    return $output
}

function Get-AndroidSdkRoot {
    param([string] $AndroidRoot)
    if ($env:ANDROID_SDK_ROOT -and (Test-Path -LiteralPath $env:ANDROID_SDK_ROOT)) {
        return [IO.Path]::GetFullPath($env:ANDROID_SDK_ROOT)
    }
    if ($env:ANDROID_HOME -and (Test-Path -LiteralPath $env:ANDROID_HOME)) {
        return [IO.Path]::GetFullPath($env:ANDROID_HOME)
    }
    $localProperties = Join-Path $AndroidRoot "local.properties"
    if (Test-Path -LiteralPath $localProperties) {
        $sdkLine = Get-Content -LiteralPath $localProperties |
            Where-Object { $_ -match '^sdk\.dir=' } |
            Select-Object -First 1
        if ($sdkLine) {
            $sdkPath = ($sdkLine -replace '^sdk\.dir=', '') -replace '\\\\', '\'
            $sdkPath = $sdkPath -replace '\\:', ':'
            if (Test-Path -LiteralPath $sdkPath) { return [IO.Path]::GetFullPath($sdkPath) }
        }
    }
    throw "Android SDK not found. Set ANDROID_SDK_ROOT or configure android/local.properties."
}

function Get-ApkPackageId {
    param([string] $Apk, [string] $AndroidRoot)
    $sdkRoot = Get-AndroidSdkRoot -AndroidRoot $AndroidRoot
    $buildToolsRoot = Join-Path $sdkRoot "build-tools"
    $aapt2 = Get-ChildItem -LiteralPath $buildToolsRoot -Directory |
        Sort-Object { [version]($_.Name -replace '-.*$', '') } -Descending |
        ForEach-Object { Join-Path $_.FullName "aapt2.exe" } |
        Where-Object { Test-Path -LiteralPath $_ } |
        Select-Object -First 1
    if (-not $aapt2) { throw "aapt2.exe not found under $buildToolsRoot" }
    $badging = & $aapt2 dump badging $Apk 2>&1
    if ($LASTEXITCODE -ne 0) { throw "Could not inspect APK package ID:`n$badging" }
    $match = [regex]::Match(($badging -join "`n"), "(?m)^package: name='([^']+)'" )
    if (-not $match.Success) { throw "aapt2 did not report a package ID for $Apk" }
    return $match.Groups[1].Value
}

function Initialize-JavaHome {
    $configuredJava = if ($env:JAVA_HOME) {
        Join-Path $env:JAVA_HOME "bin\java.exe"
    } else {
        $null
    }
    if ($configuredJava -and (Test-Path -LiteralPath $configuredJava)) { return }
    if (Get-Command java.exe -ErrorAction SilentlyContinue) { return }

    $candidates = @(
        "C:\Program Files\Android\Android Studio\jbr",
        (Join-Path $env:LOCALAPPDATA "Programs\Android Studio\jbr")
    )
    $javaHome = $candidates |
        Where-Object { Test-Path -LiteralPath (Join-Path $_ "bin\java.exe") } |
        Select-Object -First 1
    if (-not $javaHome) {
        throw "Java was not found. Install Android Studio or set JAVA_HOME to a JDK 17 installation."
    }
    $env:JAVA_HOME = $javaHome
    $env:Path = "$(Join-Path $javaHome 'bin');$env:Path"
    Write-Host "Using Android Studio Java: $javaHome"
}

$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$androidRoot = Split-Path -Parent $scriptRoot
$repoRoot = Split-Path -Parent $androidRoot
$outputRoot = [IO.Path]::GetFullPath($OutputDirectory)
New-Item -ItemType Directory -Force -Path $outputRoot | Out-Null

if (-not $TraceProcessor) {
    $perfettoToolsRoot = Join-Path $env:LOCALAPPDATA "AuraImeBenchmark\perfetto"
    $TraceProcessor = Join-Path $perfettoToolsRoot "trace_processor.py"
    if (-not (Test-Path -LiteralPath $TraceProcessor)) {
        New-Item -ItemType Directory -Force -Path $perfettoToolsRoot | Out-Null
        Write-Host "Downloading Perfetto's official trace_processor wrapper (one-time setup)..."
        try {
            Invoke-WebRequest -UseBasicParsing `
                -Uri "https://get.perfetto.dev/trace_processor" `
                -OutFile $TraceProcessor
        } catch {
            throw "Could not download Perfetto trace_processor from get.perfetto.dev: $_"
        }
    }
}
if (-not (Test-Path -LiteralPath $TraceProcessor)) {
    throw "trace_processor not found: $TraceProcessor"
}

Initialize-JavaHome

if (-not $SkipBuild) {
    Push-Location $androidRoot
    try {
        $gradleTasks = @(
            ":ime-benchmark:assembleDebug",
            ":ime-benchmark:assembleDebugAndroidTest"
        )
        if (-not $BenchmarkImeApk) { $gradleTasks += ":app:assembleProfile" }
        & .\gradlew.bat @gradleTasks -PauraImeBenchmarkTarget=true --no-daemon
        if ($LASTEXITCODE -ne 0) { throw "Isolated IME or benchmark-host build failed." }
    } finally {
        Pop-Location
    }
} else {
    Write-Host "Skipping Gradle build; validating the existing isolated APKs before installation."
}

if (-not $BenchmarkImeApk) {
    $BenchmarkImeApk = Join-Path $repoRoot "build\app\outputs\flutter-apk\app-profile.apk"
}
$benchmarkApk = Join-Path $repoRoot `
    "build\ime-benchmark\outputs\apk\debug\ime-benchmark-debug.apk"
$benchmarkTestApk = Join-Path $repoRoot `
    "build\ime-benchmark\outputs\apk\androidTest\debug\ime-benchmark-debug-androidTest.apk"
foreach ($apk in @($BenchmarkImeApk, $benchmarkApk, $benchmarkTestApk)) {
    if (-not (Test-Path -LiteralPath $apk)) { throw "Required APK not found: $apk" }
}

# This check happens before the first install. Even an accidentally supplied production APK is
# rejected while it is still only a file on the workstation.
$actualImePackage = Get-ApkPackageId -Apk $BenchmarkImeApk -AndroidRoot $androidRoot
if ($actualImePackage -eq $productionAuraPackage) {
    throw "REFUSED: production Aura APK cannot be used for an IME benchmark. Build the isolated target."
}
if ($actualImePackage -ne $benchmarkImePackage) {
    throw "REFUSED: expected benchmark package '$benchmarkImePackage', found '$actualImePackage'."
}

$devices = @(Invoke-Adb devices | Select-String "\tdevice$")
if ($devices.Count -ne 1) { throw "Connect exactly one unlocked physical Android device." }
$isEmulator = ((Invoke-Adb shell getprop ro.kernel.qemu) -join "`n").Trim()
if ($isEmulator -eq "1") { throw "The latency acceptance run requires physical hardware." }

$previousIme = ((Invoke-Adb shell settings get secure default_input_method) -join "`n").Trim()
$imeChanged = $false
$perfettoPid = $null
$primaryFailure = $null

try {
    Invoke-Adb install -r -t $BenchmarkImeApk | Out-Null
    Invoke-Adb install -r -t $benchmarkApk | Out-Null
    Invoke-Adb install -r -t $benchmarkTestApk | Out-Null

    # Only the isolated packages are reset. The production Aura package is never installed,
    # stopped, cleared, instrumented, or selected by this script.
    Invoke-Adb shell pm clear $benchmarkImePackage | Out-Null
    Invoke-Adb shell pm clear $benchmarkHostPackage | Out-Null
    Invoke-Adb shell am force-stop $benchmarkImePackage | Out-Null
    Invoke-Adb shell ime enable $benchmarkImeComponent | Out-Null
    $imeChanged = $true
    Invoke-Adb shell ime set $benchmarkImeComponent | Out-Null

    $displayDump = (Invoke-Adb shell dumpsys display) -join "`n"
    $thermalDump = (Invoke-Adb shell dumpsys thermalservice) -join "`n"
    $refreshRateMatch = [regex]::Match(
        $displayDump,
        "(?i)(?:refreshRate|renderFrameRate|fps)[=:\s]+([0-9]+(?:\.[0-9]+)?)"
    )
    $thermalStatusMatch = [regex]::Match(
        $thermalDump,
        "(?im)(?:Thermal Status|mStatus)[=:\s]+([0-9]+)"
    )
    $deviceMemInfo = Invoke-Adb shell cat /proc/meminfo
    $memTotal = (($deviceMemInfo | Select-String "^MemTotal:").Line -join "`n").Trim()
    $metadata = [ordered]@{
        mode = $Mode
        build_type = "profile"
        captured_at_utc = (Get-Date).ToUniversalTime().ToString("o")
        serial = (Invoke-Adb get-serialno) -join "`n"
        model = (Invoke-Adb shell getprop ro.product.model) -join "`n"
        soc = (Invoke-Adb shell getprop ro.soc.model) -join "`n"
        hardware = (Invoke-Adb shell getprop ro.hardware) -join "`n"
        abi = (Invoke-Adb shell getprop ro.product.cpu.abi) -join "`n"
        android_version = (Invoke-Adb shell getprop ro.build.version.release) -join "`n"
        build_fingerprint = (Invoke-Adb shell getprop ro.build.fingerprint) -join "`n"
        ram = $memTotal
        refresh_rate_hz = if ($refreshRateMatch.Success) {
            [double]$refreshRateMatch.Groups[1].Value
        } else { $null }
        thermal_status = if ($thermalStatusMatch.Success) {
            [int]$thermalStatusMatch.Groups[1].Value
        } else { $null }
        display = $displayDump
        thermal = $thermalDump
        benchmark_ime_apk_bytes = (Get-Item -LiteralPath $BenchmarkImeApk).Length
        benchmark_ime_package = $benchmarkImePackage
        production_package_refused = $productionAuraPackage
        prior_default_ime = $previousIme
        benchmark_ime_dump = (Invoke-Adb shell dumpsys package $benchmarkImePackage) -join "`n"
    }
    $metadata | ConvertTo-Json -Depth 4 | Set-Content -Encoding UTF8 `
        (Join-Path $outputRoot "device-metadata.json")

    Invoke-Adb shell dumpsys meminfo $benchmarkImePackage | Set-Content -Encoding UTF8 `
        (Join-Path $outputRoot "meminfo-before.txt")

    # Modern Android SELinux blocks Perfetto from reading shell configs in /data/local/tmp.
    # /data/misc/perfetto-configs is the platform-supported handoff directory.
    $remoteConfig = "/data/misc/perfetto-configs/aura-ime-perfetto.pbtxt"
    $remoteTrace = "/data/misc/perfetto-traces/aura-ime.perfetto-trace"
    Invoke-Adb push (Join-Path $scriptRoot "perfetto\ime_latency.pbtxt") $remoteConfig | Out-Null
    $startOutput = Invoke-Adb shell perfetto --background-wait --txt `
        -c $remoteConfig -o $remoteTrace
    $pidMatch = [regex]::Match(($startOutput -join "`n"), "(?m)^\s*(\d+)\s*$")
    if (-not $pidMatch.Success) { throw "Perfetto did not return a PID: $startOutput" }
    $perfettoPid = $pidMatch.Groups[1].Value

    $workloadOutput = Invoke-Adb shell am instrument -w `
        -e class dev.varuntej.aura.imebenchmark.ImeLatencyWorkloadTest `
        dev.varuntej.aura.imebenchmark.test/androidx.test.runner.AndroidJUnitRunner
    $workloadOutput | Set-Content -Encoding UTF8 `
        (Join-Path $outputRoot "workload-instrumentation.txt")
    $workloadPassed = [bool]($workloadOutput -match "OK \(")

    Invoke-Adb shell kill -INT $perfettoPid | Out-Null
    $perfettoPid = $null
    Start-Sleep -Seconds 3

    $localTrace = Join-Path $outputRoot "aura-ime.perfetto-trace"
    Invoke-Adb pull $remoteTrace $localTrace | Out-Null
    try {
        Invoke-Adb exec-out run-as $benchmarkHostPackage cat files/ime_benchmark_result.json |
            Set-Content -Encoding UTF8 (Join-Path $outputRoot "ime_benchmark_result.json")
    } catch {
        if (-not $workloadPassed) {
            throw "The workload failed before producing its result. See workload-instrumentation.txt. $_"
        }
        throw
    }
    Invoke-Adb shell dumpsys meminfo $benchmarkImePackage | Set-Content -Encoding UTF8 `
        (Join-Path $outputRoot "meminfo-after.txt")

    $modeName = $Mode.ToLowerInvariant()
    & $Python (Join-Path $scriptRoot "tools\parse_trace.py") `
        $localTrace $TraceProcessor (Join-Path $outputRoot "ime_benchmark_result.json") `
        (Join-Path $outputRoot "latency-report.json") --mode $modeName `
        --ime-package $benchmarkImePackage
    if ($LASTEXITCODE -ne 0) { throw "One or more physical acceptance gates failed." }
    if (-not $workloadPassed) {
        throw "Instrumentation reported a failure. See workload-instrumentation.txt."
    }
} catch {
    $primaryFailure = $_
} finally {
    if ($perfettoPid) {
        try { Invoke-Adb shell kill -INT $perfettoPid | Out-Null } catch { Write-Warning $_ }
    }
    if ($imeChanged) {
        try {
            if ($previousIme -and $previousIme -ne "null") {
                Invoke-Adb shell ime set $previousIme | Out-Null
            } else {
                Invoke-Adb shell ime reset | Out-Null
            }
            Invoke-Adb shell ime disable $benchmarkImeComponent | Out-Null
            $restoredIme = ((Invoke-Adb shell settings get secure default_input_method) -join "`n").Trim()
            if ($previousIme -and $previousIme -ne "null" -and $restoredIme -ne $previousIme) {
                throw "Default IME restoration failed: expected '$previousIme', found '$restoredIme'."
            }
        } catch {
            if ($primaryFailure) {
                throw "$primaryFailure`nAdditionally, keyboard restoration failed: $_"
            }
            throw
        }
    }
}

if ($primaryFailure) { throw $primaryFailure }
