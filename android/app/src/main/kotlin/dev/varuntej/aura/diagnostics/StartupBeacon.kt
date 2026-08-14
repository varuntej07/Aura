package dev.varuntej.aura.diagnostics

import java.net.HttpURLConnection
import java.net.URL
import java.util.concurrent.Executors
import org.json.JSONObject

/**
 * Reports a failed startup to the backend using nothing but the Java standard
 * library.
 *
 * Every richer channel is unavailable at the moment we need to report:
 * Crashlytics may not be armed, PostHog needs the Flutter engine, and any
 * authenticated call needs a Firebase session the user may never have obtained
 * (the reported user had never completed a sign-in on Android at all). So this
 * uses a raw [HttpURLConnection] on a plain thread, started from
 * `MainActivity.onCreate`, with no Flutter, no Firebase, no plugins and no auth
 * in the path. It is the only reporting mechanism that still works when the app
 * dies before it can speak for itself.
 *
 * Fire-and-forget by design: the result is ignored, failures are swallowed, and
 * nothing here can block or crash startup. A diagnostic that can break the app
 * is worse than no diagnostic.
 *
 * Sends ONLY when the previous launch failed to reach a first frame, so a
 * healthy install generates zero traffic for the lifetime of the app.
 */
object StartupBeacon {

    /**
     * Both `Env.dev` and `Env.prod` in `environment.dart` resolve to this same
     * Cloud Run service, so there is one correct value regardless of how the
     * Dart side is compiled. Hardcoded because this runs before any Dart config
     * is reachable.
     */
    private const val ENDPOINT = "https://juno-backend-620715294422.us-central1.run.app/diagnostics/startup"

    private const val CONNECT_TIMEOUT_MS = 5_000
    private const val READ_TIMEOUT_MS = 5_000

    /**
     * A single-thread executor rather than a bare `Thread`, so a device stuck in
     * a crash loop cannot spawn an unbounded number of sockets.
     */
    private val executor = Executors.newSingleThreadExecutor { runnable ->
        Thread(runnable, "aura-startup-beacon").apply { isDaemon = true }
    }

    /**
     * Posts [report] on a background thread. Returns immediately.
     *
     * Takes no Context deliberately: holding one from a startup path that may be
     * about to die risks leaking an Activity for the lifetime of the request,
     * and everything context-derived is already baked into [report] by the time
     * this is called.
     */
    fun send(report: JSONObject) {
        val body = try {
            report.toString()
        } catch (_: Throwable) {
            return
        }
        executor.execute {
            var connection: HttpURLConnection? = null
            try {
                connection = (URL(ENDPOINT).openConnection() as HttpURLConnection).apply {
                    requestMethod = "POST"
                    connectTimeout = CONNECT_TIMEOUT_MS
                    readTimeout = READ_TIMEOUT_MS
                    doOutput = true
                    setRequestProperty("Content-Type", "application/json")
                }
                connection.outputStream.use { it.write(body.toByteArray(Charsets.UTF_8)) }
                // Reading the status code is what actually flushes the request.
                connection.responseCode
            } catch (_: Throwable) {
                // Offline, DNS failure, backend down: all expected and all
                // ignorable. The breadcrumb stays on disk and the next launch
                // tries again.
            } finally {
                try {
                    connection?.disconnect()
                } catch (_: Throwable) {
                }
            }
        }
    }
}
