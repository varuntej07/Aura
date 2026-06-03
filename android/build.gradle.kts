import com.android.build.gradle.BaseExtension

allprojects {
    repositories {
        google()
        mavenCentral()
    }
}

val newBuildDir: Directory =
    rootProject.layout.buildDirectory
        .dir("../../build")
        .get()
rootProject.layout.buildDirectory.value(newBuildDir)

subprojects {
    val newSubprojectBuildDir: Directory = newBuildDir.dir(project.name)
    project.layout.buildDirectory.value(newSubprojectBuildDir)
}

subprojects {
    tasks.withType<org.jetbrains.kotlin.gradle.tasks.KotlinCompile>().configureEach {
        compilerOptions {
            languageVersion.set(org.jetbrains.kotlin.gradle.dsl.KotlinVersion.KOTLIN_1_9)
        }
    }
    // Plugins set their own Java/Kotlin JVM targets, and they disagree: some pin
    // Kotlin high (file_picker -> 21), some pin Java low (flutter_image_compress_common -> 11), 
    // some pin Kotlin low (posthog_flutter -> 1.8). The Kotlin Gradle plugin then fails 
    // the build whenever a module's Java and Kotlin targets don't match. 
    // Force BOTH sides to 17 on every Android module so the pair is always consistent, regardless of what each plugin declares.
    //
    // This MUST run in afterEvaluate, and this block MUST be registered before
    // the evaluationDependsOn(":app") block below, for two reasons:
    //   1. A plugin sets its targets during its own evaluation, so an override
    //      registered at root-config time loses to the plugin's later value.
    //      afterEvaluate runs after the plugin is fully evaluated, so the Java
    //      compileOptions override and the Kotlin configureEach registered here
    //      are the last writers and win at task realization.
    //   2. Evaluating :app makes the Flutter Gradle plugin eagerly evaluate
    //      every plugin module. If this afterEvaluate were registered after the
    //      evaluationDependsOn block, those modules would already be evaluated
    //      and Gradle would throw "Cannot run afterEvaluate ... already evaluated".
    afterEvaluate {
        val androidExtension = extensions.findByName("android") as? BaseExtension
            ?: return@afterEvaluate
        androidExtension.compileOptions {
            sourceCompatibility = JavaVersion.VERSION_17
            targetCompatibility = JavaVersion.VERSION_17
        }
        tasks.withType<org.jetbrains.kotlin.gradle.tasks.KotlinCompile>().configureEach {
            compilerOptions {
                jvmTarget.set(org.jetbrains.kotlin.gradle.dsl.JvmTarget.JVM_17)
            }
        }
    }
}

subprojects {
    project.evaluationDependsOn(":app")
}

tasks.register<Delete>("clean") {
    delete(rootProject.layout.buildDirectory)
}
