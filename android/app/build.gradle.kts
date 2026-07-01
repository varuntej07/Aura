import java.util.Properties
import java.io.FileInputStream

plugins {
    id("com.android.application")
    // FlutterFire Configuration
    id("com.google.gms.google-services")
    id("com.google.firebase.crashlytics")

    // The Flutter Gradle Plugin must be applied after the Android and Kotlin Gradle plugins.
    id("dev.flutter.flutter-gradle-plugin")
}

val keyPropertiesFile = rootProject.file("key.properties")
val keyProperties = Properties()
// Only the release build needs the signing keystore. Loading it unconditionally made EVERY build
// (debug, CI, a fresh clone) fail at configuration time when key.properties is absent; gate it so
// only release signing depends on the file being present.
val hasReleaseSigningConfig = keyPropertiesFile.exists()
if (hasReleaseSigningConfig) {
    keyProperties.load(FileInputStream(keyPropertiesFile))
}

android {
    namespace = "dev.varuntej.aura"
    compileSdk = flutter.compileSdkVersion
    ndkVersion = flutter.ndkVersion

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
        isCoreLibraryDesugaringEnabled = true
    }

    signingConfigs {
        if (hasReleaseSigningConfig) {
            create("release") {
                keyAlias = keyProperties["keyAlias"] as String
                keyPassword = keyProperties["keyPassword"] as String
                storeFile = file(keyProperties["storeFile"] as String)
                storePassword = keyProperties["storePassword"] as String
            }
        }
    }

    defaultConfig {
        // TODO: Specify your own unique Application ID (https://developer.android.com/studio/build/application-id.html).
        applicationId = "dev.varuntej.aura"
        // You can update the following values to match your application needs.
        // For more information, see: https://flutter.dev/to/review-gradle-config.
        minSdk = flutter.minSdkVersion
        targetSdk = flutter.targetSdkVersion
        versionCode = flutter.versionCode
        versionName = flutter.versionName
    }

    buildTypes {
        release {
            // With key.properties present (a real release run) this signs as before; without it
            // (CI / fresh clone) the release build stays unsigned instead of failing configuration.
            if (hasReleaseSigningConfig) {
                signingConfig = signingConfigs.getByName("release")
            }
            isMinifyEnabled = true
            isShrinkResources = true
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro"
            )
        }
    }
}

flutter {
    source = "../.."
}

kotlin {
    compilerOptions {
        jvmTarget = org.jetbrains.kotlin.gradle.dsl.JvmTarget.JVM_17
    }
}

dependencies {
    coreLibraryDesugaring("com.android.tools:desugar_jdk_libs:2.1.4")
    // EncryptedSharedPreferences for the Buddy Keyboard credential bridge: the app
    // writes a revocable keyboard credential the IME (same UID, separate process)
    // reads to authenticate /keyboard/draft. 1.1.0-alpha06 is the only release line
    // that ships the MasterKey.Builder API (security-crypto never had a stable 1.1.0).
    implementation("androidx.security:security-crypto:1.1.0-alpha06")

    // Native Firebase Auth for the Buddy Keyboard IME. The keyboard runs in its own
    // process and mints a FRESH Firebase ID token on demand (FirebaseAuth.getIdToken)
    // rather than reading the app-bridged token off disk, which goes stale whenever the
    // app hasn't run in the last hour (the keyboard's normal state). The firebase_auth
    // Flutter plugin pulls firebase-auth as `implementation`, so it sits on the runtime
    // classpath but NOT the app module's compile classpath; this BoM, pinned to the exact
    // version firebase_core 4.11.0 resolves (34.15.0), exposes it to compile with zero
    // version skew (Gradle dedupes to the single resolved artifact).
    implementation(platform("com.google.firebase:firebase-bom:34.15.0"))
    implementation("com.google.firebase:firebase-auth")

    // JVM unit tests for the keyboard's pure logic (FieldProfile field detection,
    // StrongPassword generation). Test classpath only; no effect on the app artifact.
    testImplementation("junit:junit:4.13.2")

    // In-keyboard voice: native LiveKit Android SDK so the IME holds a WebRTC duplex to
    // the voice agent IN-PROCESS (tap mic in the keyboard -> talk to Buddy without
    // leaving the host app). It pulls io.github.webrtc-sdk:android-PREFIXED, a distinct
    // native lib from flutter_webrtc's io.github.webrtc-sdk:android (prefixed symbols +
    // separate .so), so it coexists with the app's existing voice with zero collision
    // (verified: assembleDebug packages both). Costs APK size (a second WebRTC .so).
    implementation("io.livekit:livekit-android:2.26.0")
}
