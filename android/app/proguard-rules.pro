# Flutter — keep plugin registry and platform channel classes
-keep class io.flutter.** { *; }
-keep class io.flutter.plugins.** { *; }
-keep class io.flutter.plugin.** { *; }

# WebRTC (livekit_client) — JNI-bound classes must not be renamed
-keep class org.webrtc.** { *; }
-dontwarn org.webrtc.**

# SQLite / Drift — generated code uses reflection for column mapping
-keep class ** extends androidx.sqlite.db.SupportSQLiteOpenHelper { *; }
-keep class ** extends androidx.room.RoomDatabase { *; }
-dontwarn androidx.room.**

# In-app purchase — billing client uses reflection for purchase verification
-keep class com.android.billingclient.** { *; }
-dontwarn com.android.billingclient.**

# Kotlin metadata — required for kotlin-reflect and coroutines
-keep class kotlin.Metadata { *; }
-dontwarn kotlin.**
-dontwarn kotlinx.**

# Flutter deferred components (Play Core split install) — not used in this app
-dontwarn com.google.android.play.core.splitcompat.SplitCompatApplication
-dontwarn com.google.android.play.core.splitinstall.SplitInstallException
-dontwarn com.google.android.play.core.splitinstall.SplitInstallManager
-dontwarn com.google.android.play.core.splitinstall.SplitInstallManagerFactory
-dontwarn com.google.android.play.core.splitinstall.SplitInstallRequest$Builder
-dontwarn com.google.android.play.core.splitinstall.SplitInstallRequest
-dontwarn com.google.android.play.core.splitinstall.SplitInstallSessionState
-dontwarn com.google.android.play.core.splitinstall.SplitInstallStateUpdatedListener
-dontwarn com.google.android.play.core.tasks.OnFailureListener
-dontwarn com.google.android.play.core.tasks.OnSuccessListener
-dontwarn com.google.android.play.core.tasks.Task
