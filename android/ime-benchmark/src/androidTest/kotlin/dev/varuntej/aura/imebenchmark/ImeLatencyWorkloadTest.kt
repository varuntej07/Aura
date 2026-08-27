package dev.varuntej.aura.imebenchmark

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.AccessibilityServiceInfo
import android.app.UiAutomation
import android.content.Intent
import android.graphics.Rect
import android.os.SystemClock
import android.os.Trace
import android.provider.Settings
import android.view.InputDevice
import android.view.MotionEvent
import android.view.accessibility.AccessibilityNodeInfo
import androidx.test.core.app.ActivityScenario
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import java.security.MessageDigest
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class ImeLatencyWorkloadTest {
    private val instrumentation = InstrumentationRegistry.getInstrumentation()
    private val automation = instrumentation.uiAutomation

    @Test
    fun replayCoreTypingLoop_withoutDropDuplicateOrReorder() {
        ActivityScenario.launch(BenchmarkActivity::class.java).use { scenario ->
            configureWindowInspection(automation)
            SystemClock.sleep(MODEL_WARMUP_MS)

            val workload = buildSustainedWorkload()
            val requiredDescriptions = workload.mapTo(linkedSetOf(), Event::description).apply {
                addAll(
                    listOf(
                        "aura_key_shift",
                        "aura_key_enter",
                        "aura_key_backspace",
                        "aura_key_space",
                    ),
                )
            }
            var keyCenters = awaitKeyCenters(requiredDescriptions)
            val characterEventIndices = mutableListOf<Long>()
            var nextEventIndex = 1L

            scenario.onActivity(BenchmarkActivity::captureImeRuntimeSnapshot)
            SystemClock.sleep(RUNTIME_SNAPSHOT_SETTLE_MS)

            workload.forEachIndexed { index, event ->
                if (event.kind == EventKind.CHARACTER) characterEventIndices += nextEventIndex
                injectTap(nextEventIndex++, keyCenters.getValue(event.description))
                SystemClock.sleep(event.delayAfterMs ?: INTER_KEY_MS[index % INTER_KEY_MS.size])
            }
            SystemClock.sleep(SCENARIO_SETTLE_MS)
            val expectedSustained = expectedText(workload)
            val actualSustained = primaryText(scenario)
            val sustainedMatches = expectedSustained.lowercase() == actualSustained.lowercase()

            resetAndFocus(scenario, primary = true)
            "abcdef".forEach { character ->
                characterEventIndices += nextEventIndex
                injectTap(nextEventIndex++, keyCenters.getValue(descriptionFor(character)))
                SystemClock.sleep(BURST_INTERVAL_MS)
            }
            repeat(BACKSPACE_BURST_COUNT) {
                injectTap(nextEventIndex++, keyCenters.getValue("aura_key_backspace"))
                SystemClock.sleep(BURST_INTERVAL_MS)
            }
            SystemClock.sleep(SCENARIO_SETTLE_MS)
            val backspaceBurstCorrect = primaryText(scenario).isEmpty()

            resetAndFocus(scenario, primary = false)
            val capitalizationAndNewline = listOf(
                "aura_key_char_a",
                "aura_key_char_b",
                "aura_key_space",
                "aura_key_shift",
                "aura_key_char_c",
                "aura_key_enter",
                "aura_key_char_d",
            )
            capitalizationAndNewline.forEach { description ->
                if (description.startsWith("aura_key_char_")) characterEventIndices += nextEventIndex
                injectTap(nextEventIndex++, keyCenters.getValue(description))
                SystemClock.sleep(BURST_INTERVAL_MS)
            }
            SystemClock.sleep(SCENARIO_SETTLE_MS)
            val capitalizationNewlineCorrect = secondaryText(scenario) == "Ab C\nD"

            resetAndFocus(scenario, primary = true)
            "one".forEach { character ->
                characterEventIndices += nextEventIndex
                injectTap(nextEventIndex++, keyCenters.getValue(descriptionFor(character)))
                SystemClock.sleep(BURST_INTERVAL_MS)
            }
            scenario.onActivity(BenchmarkActivity::focusSecondary)
            SystemClock.sleep(FIELD_SWITCH_SETTLE_MS)
            keyCenters = awaitKeyCenters(requiredDescriptions)
            "two".forEach { character ->
                characterEventIndices += nextEventIndex
                injectTap(nextEventIndex++, keyCenters.getValue(descriptionFor(character)))
                SystemClock.sleep(BURST_INTERVAL_MS)
            }
            SystemClock.sleep(SCENARIO_SETTLE_MS)
            val fieldsCorrect = primaryText(scenario).lowercase() == "one" &&
                secondaryText(scenario).lowercase() == "two"

            resetAndFocus(scenario, primary = true)
            characterEventIndices += nextEventIndex
            injectTap(nextEventIndex++, keyCenters.getValue("aura_key_char_a"))
            SystemClock.sleep(SCENARIO_SETTLE_MS)
            instrumentation.targetContext.startActivity(
                Intent(Settings.ACTION_SETTINGS).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
            )
            SystemClock.sleep(APP_SWITCH_SETTLE_MS)
            val returnedFromOtherApp = automation.performGlobalAction(
                AccessibilityService.GLOBAL_ACTION_BACK,
            )
            SystemClock.sleep(APP_SWITCH_SETTLE_MS)
            scenario.onActivity(BenchmarkActivity::focusPrimary)
            keyCenters = awaitKeyCenters(requiredDescriptions)
            characterEventIndices += nextEventIndex
            injectTap(nextEventIndex++, keyCenters.getValue("aura_key_char_b"))
            SystemClock.sleep(SCENARIO_SETTLE_MS)
            val appSwitchCorrect = returnedFromOtherApp && primaryText(scenario).lowercase() == "ab"

            scenario.onActivity(BenchmarkActivity::captureImeRuntimeSnapshot)
            SystemClock.sleep(RUNTIME_SNAPSHOT_SETTLE_MS)
            SystemClock.sleep(FINAL_FRAME_SETTLE_MS)

            val scenarios = JSONObject()
                .put("sustained_fast_typing", sustainedMatches)
                .put("backspace_burst", backspaceBurstCorrect)
                .put("capitalization_and_newline", capitalizationNewlineCorrect)
                .put("switching_fields", fieldsCorrect)
                .put("switching_apps", appSwitchCorrect)
            val allScenariosPassed = listOf(
                sustainedMatches,
                backspaceBurstCorrect,
                capitalizationNewlineCorrect,
                fieldsCorrect,
                appSwitchCorrect,
            ).all { it }
            val report = JSONObject()
                .put("event_count", nextEventIndex - 1)
                .put("sustained_event_count", workload.size)
                .put("character_event_indices", JSONArray(characterEventIndices))
                .put("inter_key_ms", JSONArray(INTER_KEY_MS.toList()))
                .put("correction_settle_ms", CORRECTION_SETTLE_MS)
                .put("correction_samples", PHRASE_REPETITIONS)
                .put("expected_utf8_sha256", sha256(expectedSustained.lowercase()))
                .put("actual_utf8_sha256", sha256(actualSustained.lowercase()))
                .put("dropped_duplicated_or_reordered", !sustainedMatches)
                .put("scenarios", scenarios)
                .put("all_scenarios_passed", allScenariosPassed)
            scenario.onActivity { activity ->
                activity.filesDir.resolve(RESULT_FILE).writeText(report.toString(2) + "\n")
            }
            assertTrue("One or more IME core-loop scenarios failed: $scenarios", allScenariosPassed)
        }
    }

    private fun buildSustainedWorkload(): List<Event> = buildList {
        repeat(PHRASE_REPETITIONS) {
            PHRASE.forEachIndexed { phraseIndex, character ->
                if (phraseIndex > TYPO_SEPARATOR_INDEX && size > 0 && size % EDIT_INTERVAL == 0) {
                    add(Event("aura_key_char_x", 'x', EventKind.CHARACTER))
                    add(Event("aura_key_backspace", null, EventKind.BACKSPACE, deletes = true))
                }
                add(
                    Event(
                        description = descriptionFor(character),
                        character = character,
                        kind = if (character == ' ') EventKind.FUNCTION else EventKind.CHARACTER,
                        delayAfterMs = if (phraseIndex == TYPO_LAST_CHARACTER_INDEX) {
                            CORRECTION_SETTLE_MS
                        } else {
                            null
                        },
                        correctionRaw = if (phraseIndex == TYPO_SEPARATOR_INDEX) TYPO_RAW else null,
                        correctionFinal = if (phraseIndex == TYPO_SEPARATOR_INDEX) TYPO_FINAL else null,
                    ),
                )
            }
        }
    }

    private fun expectedText(workload: List<Event>): String = buildString {
        workload.forEach { event ->
            when {
                event.deletes && isNotEmpty() -> deleteCharAt(lastIndex)
                event.character != null -> {
                    if (event.correctionRaw != null && event.correctionFinal != null) {
                        check(endsWith(event.correctionRaw))
                        delete(lastIndex - event.correctionRaw.lastIndex, length)
                        append(event.correctionFinal)
                    }
                    append(event.character)
                }
            }
        }
    }

    private fun resetAndFocus(
        scenario: ActivityScenario<BenchmarkActivity>,
        primary: Boolean,
    ) {
        scenario.onActivity { activity ->
            activity.clearEditors()
            if (primary) activity.focusPrimary() else activity.focusSecondary()
        }
        SystemClock.sleep(FIELD_SWITCH_SETTLE_MS)
    }

    private fun primaryText(scenario: ActivityScenario<BenchmarkActivity>): String {
        var value = ""
        scenario.onActivity { value = it.editor.text.toString() }
        return value
    }

    private fun secondaryText(scenario: ActivityScenario<BenchmarkActivity>): String {
        var value = ""
        scenario.onActivity { value = it.secondaryEditor.text.toString() }
        return value
    }

    private fun descriptionFor(character: Char): String = when (character) {
        ' ' -> "aura_key_space"
        else -> "aura_key_char_$character"
    }

    private fun configureWindowInspection(uiAutomation: UiAutomation) {
        val info = uiAutomation.serviceInfo
        info.flags = info.flags or AccessibilityServiceInfo.FLAG_RETRIEVE_INTERACTIVE_WINDOWS
        uiAutomation.serviceInfo = info
    }

    private fun awaitKeyCenters(required: Set<String>): Map<String, Pair<Float, Float>> {
        val deadline = SystemClock.uptimeMillis() + KEY_DISCOVERY_TIMEOUT_MS
        while (SystemClock.uptimeMillis() < deadline) {
            val found = LinkedHashMap<String, Pair<Float, Float>>()
            automation.windows.forEach { window ->
                window.root?.let { collectCenters(it, required, found) }
            }
            if (found.keys.containsAll(required)) return found
            SystemClock.sleep(100)
        }
        error("Aura IME keys were not visible: $required")
    }

    private fun collectCenters(
        root: AccessibilityNodeInfo,
        required: Set<String>,
        output: MutableMap<String, Pair<Float, Float>>,
    ) {
        val queue = ArrayDeque<AccessibilityNodeInfo>()
        queue.add(root)
        val bounds = Rect()
        while (queue.isNotEmpty()) {
            val node = queue.removeFirst()
            val description = node.contentDescription?.toString()
                ?: legacyDescription(node.text?.toString())
            if (description in required && node.isVisibleToUser) {
                node.getBoundsInScreen(bounds)
                if (!bounds.isEmpty) output[description!!] = bounds.exactCenterX() to bounds.exactCenterY()
            }
            repeat(node.childCount) { index -> node.getChild(index)?.let(queue::addLast) }
        }
    }

    private fun injectTap(eventIndex: Long, center: Pair<Float, Float>) {
        BenchmarkEventSequence.injectedEvent = eventIndex
        val downTime = SystemClock.uptimeMillis()
        val down = motion(downTime, downTime, MotionEvent.ACTION_DOWN, center.first, center.second)
        try {
            tracePoint(INJECTED_ACTION_DOWN_SECTION)
            automation.injectInputEvent(down, true)
        } finally {
            down.recycle()
        }
        val upTime = downTime + TOUCH_HOLD_MS
        val up = motion(downTime, upTime, MotionEvent.ACTION_UP, center.first, center.second)
        try {
            tracePoint(INJECTED_ACTION_UP_SECTION)
            automation.injectInputEvent(up, true)
        } finally {
            up.recycle()
        }
    }

    private fun tracePoint(name: String) {
        if (android.os.Build.VERSION.SDK_INT >= android.os.Build.VERSION_CODES.Q && Trace.isEnabled()) {
            Trace.beginSection(name)
            Trace.endSection()
        }
    }

    private fun motion(
        downTime: Long,
        eventTime: Long,
        action: Int,
        x: Float,
        y: Float,
    ): MotionEvent = MotionEvent.obtain(downTime, eventTime, action, x, y, 0).apply {
        source = InputDevice.SOURCE_TOUCHSCREEN
    }

    private fun legacyDescription(label: String?): String? {
        if (label.isNullOrBlank()) return null
        val normalized = label.lowercase()
        return when {
            normalized.length == 1 && (normalized[0].isLetter() || normalized in ".,") ->
                "aura_key_char_$normalized"
            normalized in setOf("buddy", "english") -> "aura_key_space"
            normalized in setOf("⌫", "âŒ«") -> "aura_key_backspace"
            normalized in setOf("⇧", "â‡§") -> "aura_key_shift"
            normalized in setOf("↵", "â†µ") -> "aura_key_enter"
            else -> null
        }
    }

    private fun sha256(value: String): String = MessageDigest.getInstance("SHA-256")
        .digest(value.toByteArray())
        .joinToString("") { "%02x".format(it) }

    private enum class EventKind { CHARACTER, BACKSPACE, FUNCTION }

    private data class Event(
        val description: String,
        val character: Char?,
        val kind: EventKind,
        val deletes: Boolean = false,
        val delayAfterMs: Long? = null,
        val correctionRaw: String? = null,
        val correctionFinal: String? = null,
    )

    private companion object {
        const val TYPO_RAW = "teh"
        const val TYPO_FINAL = "the"
        const val TYPO_LAST_CHARACTER_INDEX = 2
        const val TYPO_SEPARATOR_INDEX = 3
        const val PHRASE = "teh quick brown fox jumps over the lazy dog. aura helps me write, without delay. "
        const val PHRASE_REPETITIONS = 32
        const val EDIT_INTERVAL = 89
        const val BACKSPACE_BURST_COUNT = 6
        const val MODEL_WARMUP_MS = 5_000L
        const val FINAL_FRAME_SETTLE_MS = 2_000L
        const val RUNTIME_SNAPSHOT_SETTLE_MS = 100L
        const val SCENARIO_SETTLE_MS = 350L
        const val FIELD_SWITCH_SETTLE_MS = 500L
        const val APP_SWITCH_SETTLE_MS = 1_000L
        const val KEY_DISCOVERY_TIMEOUT_MS = 10_000L
        const val TOUCH_HOLD_MS = 4L
        const val BURST_INTERVAL_MS = 24L
        const val CORRECTION_SETTLE_MS = 240L
        val INTER_KEY_MS = longArrayOf(24, 28, 32, 36, 42, 50)
        const val RESULT_FILE = "ime_benchmark_result.json"
        const val INJECTED_ACTION_DOWN_SECTION = "AuraBench:injected ACTION_DOWN"
        const val INJECTED_ACTION_UP_SECTION = "AuraBench:injected ACTION_UP"
    }
}
