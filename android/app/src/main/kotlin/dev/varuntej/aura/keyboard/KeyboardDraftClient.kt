package dev.varuntej.aura.keyboard

import android.os.Handler
import android.os.Looper
import org.json.JSONArray
import org.json.JSONObject
import java.io.BufferedReader
import java.net.HttpURLConnection
import java.net.URL
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicLong

/**
 * Calls POST /keyboard/draft and returns Buddy-voiced suggestions for the tapped
 * action. Plain HttpURLConnection + org.json (both in the Android framework), so the
 * keyboard stays dependency-free.
 *
 * Latency is a feature: the request runs off the UI thread, the callback is delivered
 * back on the main thread, and every failure path resolves to a coded Result so the
 * Buddy bar can show a bounded graceful state and never hangs.
 */
object KeyboardDraftClient {

    // A cached pool, not a single thread: a newly-tapped action starts at once instead of waiting
    // out the previous action's full socket timeout (head-of-line blocking). Threads are reaped
    // when idle; the request rate is human taps, so this stays tiny.
    private val executor = Executors.newCachedThreadPool()
    private val mainHandler = Handler(Looper.getMainLooper())

    // Monotonic id per draft call. Only the latest request's result is delivered (last-write-wins),
    // so a slow earlier action can never overwrite a newer action's suggestions in the bar.
    private val requestCounter = AtomicLong(0)

    // Socket budgets sit just above the backend's 6s draft budget
    // (KEYBOARD_DRAFT_TIMEOUT_SECONDS), so a near-the-limit draft still arrives
    // instead of being severed by the socket.
    private const val CONNECT_TIMEOUT_MS = 8_000
    private const val READ_TIMEOUT_MS = 9_000

    // The user-facing bound on the "thinking" state, independent of (and tighter than) the sum of
    // the socket timeouts, so the bar always resolves to a result or a graceful timeout in time.
    private const val OVERALL_DEADLINE_MS = 9_000L

    sealed class Result {
        data class Success(val suggestions: List<String>, val reason: String) : Result()
        data class Failure(val reason: String) : Result()
        /** No signed-in credential is shared yet: prompt the user to open Aura. */
        object NoCredential : Result()
    }

    fun draft(
        credential: KeyboardCredentialStore.Credential?,
        action: String,
        contextBefore: String,
        hostApp: String?,
        n: Int = 3,
        tone: String? = null,
        targetLang: String? = null,
        fieldType: String? = null,
        onResult: (Result) -> Unit,
    ) {
        if (credential == null) {
            onResult(Result.NoCredential)
            return
        }
        val requestId = requestCounter.incrementAndGet()
        val delivered = AtomicBoolean(false)
        // Deliver to the bar at most once, and only while this is still the most recent draft. A
        // superseded (older) request is silently dropped so it cannot clobber the newer one.
        fun deliver(result: Result) {
            if (delivered.compareAndSet(false, true) && requestId == requestCounter.get()) {
                onResult(result)
            }
        }
        // Hard user-facing deadline, separate from the socket timeouts, so the "thinking" state can
        // never hang past OVERALL_DEADLINE_MS even if the sockets would wait longer.
        val deadlineRunnable = Runnable { deliver(Result.Failure("timeout")) }
        mainHandler.postDelayed(deadlineRunnable, OVERALL_DEADLINE_MS)
        executor.execute {
            val result = try {
                request(credential, action, contextBefore, hostApp, n, tone, targetLang, fieldType)
            } catch (t: Throwable) {
                Result.Failure("network_error")
            }
            mainHandler.post {
                mainHandler.removeCallbacks(deadlineRunnable)
                deliver(result)
            }
        }
    }

    private fun request(
        credential: KeyboardCredentialStore.Credential,
        action: String,
        contextBefore: String,
        hostApp: String?,
        n: Int,
        tone: String?,
        targetLang: String?,
        fieldType: String?,
    ): Result {
        val endpoint = credential.apiBaseUrl.trimEnd('/') + "/keyboard/draft"
        val conn = (URL(endpoint).openConnection() as HttpURLConnection).apply {
            requestMethod = "POST"
            connectTimeout = CONNECT_TIMEOUT_MS
            readTimeout = READ_TIMEOUT_MS
            doOutput = true
            setRequestProperty("Content-Type", "application/json")
            setRequestProperty("Authorization", "Bearer ${credential.idToken}")
        }

        // Field names mirror DraftRequest in services/keyboard/drafter.py exactly.
        val body = JSONObject().apply {
            put("action", action)
            put("context_before", contextBefore)
            put("n", n)
            if (!hostApp.isNullOrBlank()) put("host_app", hostApp)
            if (!tone.isNullOrBlank()) put("tone", tone)
            if (!targetLang.isNullOrBlank()) put("target_lang", targetLang)
            if (!fieldType.isNullOrBlank()) put("field_type", fieldType)
        }

        try {
            conn.outputStream.use { it.write(body.toString().toByteArray(Charsets.UTF_8)) }

            val code = conn.responseCode
            if (code != HttpURLConnection.HTTP_OK) {
                // Drain the error body so the underlying socket can be reused (keep-alive) rather
                // than torn down on every non-2xx.
                conn.errorStream?.use { it.readBytes() }
                return Result.Failure(if (code == 401) "unauthorized" else "http_$code")
            }

            val text = conn.inputStream.bufferedReader().use(BufferedReader::readText)
            val json = JSONObject(text)
            val arr: JSONArray = json.optJSONArray("suggestions") ?: JSONArray()
            val suggestions = (0 until arr.length()).mapNotNull { i ->
                arr.optString(i).takeIf { it.isNotBlank() }
            }
            val reason = json.optString("reason", "ok")
            return Result.Success(suggestions, reason)
        } finally {
            conn.disconnect()
        }
    }
}
