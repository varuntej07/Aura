package dev.varuntej.aura.imebenchmark

import android.app.Activity
import android.content.Context
import android.graphics.Canvas
import android.graphics.Typeface
import android.os.Build
import android.os.Bundle
import android.os.Trace
import android.text.Editable
import android.text.InputType
import android.text.TextWatcher
import android.view.Gravity
import android.view.WindowManager
import android.view.inputmethod.InputMethodManager
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.TextView

object BenchmarkEventSequence {
    @Volatile
    var injectedEvent: Long = 0

    @Volatile
    private var mutationSequence: Long = 0

    fun nextMutation(): Long = synchronized(this) { ++mutationSequence }
}

class BenchmarkActivity : Activity() {
    lateinit var editor: PresentedGlyphEditText
        private set
    lateinit var secondaryEditor: PresentedGlyphEditText
        private set

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        val density = resources.displayMetrics.density
        val status = TextView(this).apply {
            text = "Aura IME physical latency workload"
            textSize = 16f
            gravity = Gravity.CENTER_VERTICAL
            setPadding((16 * density).toInt(), (12 * density).toInt(), 0, (8 * density).toInt())
        }
        editor = benchmarkEditor(android.R.id.edit, minLines = 5)
        secondaryEditor = benchmarkEditor(SECONDARY_EDITOR_ID, minLines = 2)
        setContentView(LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            addView(status, LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT)
            addView(editor, LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, 0, 2f,
            ))
            addView(secondaryEditor, LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, 0, 1f,
            ))
        })
        focusEditor(editor)
    }

    private fun benchmarkEditor(editorId: Int, minLines: Int) = PresentedGlyphEditText(this).apply {
            id = editorId
            textSize = 24f
            typeface = Typeface.MONOSPACE
            gravity = Gravity.TOP or Gravity.START
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_FLAG_MULTI_LINE or
                InputType.TYPE_TEXT_FLAG_AUTO_CORRECT or InputType.TYPE_TEXT_FLAG_CAP_SENTENCES
            this.minLines = minLines
            val density = resources.displayMetrics.density
            setPadding(
                (16 * density).toInt(),
                (12 * density).toInt(),
                (16 * density).toInt(),
                (12 * density).toInt(),
            )
        }

    fun focusPrimary() = focusEditor(editor)

    fun focusSecondary() = focusEditor(secondaryEditor)

    fun clearEditors() {
        BenchmarkEventSequence.injectedEvent = 0
        editor.text.clear()
        secondaryEditor.text.clear()
    }

    private fun focusEditor(target: EditText) {
        target.requestFocus()
        target.setSelection(target.text.length)
        target.post {
            (getSystemService(INPUT_METHOD_SERVICE) as InputMethodManager)
                .showSoftInput(target, InputMethodManager.SHOW_IMPLICIT)
        }
    }

    fun captureImeRuntimeSnapshot() {
        (getSystemService(INPUT_METHOD_SERVICE) as InputMethodManager).sendAppPrivateCommand(
            editor,
            PERFORMANCE_SNAPSHOT_COMMAND,
            Bundle.EMPTY,
        )
    }

    private companion object {
        const val SECONDARY_EDITOR_ID = 0x0A11A002
        const val PERFORMANCE_SNAPSHOT_COMMAND =
            "dev.varuntej.aura.keyboard.PERFORMANCE_SNAPSHOT"
    }
}

/** Marks the exact host draw that first contains each observed text mutation. */
class PresentedGlyphEditText(context: Context) : EditText(context) {
    private var mutationSequence = 0L
    private var pendingMutation = 0L
    private var pendingEvent = 0L
    private var tracedMutation = 0L

    init {
        addTextChangedListener(object : TextWatcher {
            override fun beforeTextChanged(s: CharSequence?, start: Int, count: Int, after: Int) {}
            override fun onTextChanged(s: CharSequence?, start: Int, before: Int, count: Int) {
                mutationSequence = BenchmarkEventSequence.nextMutation()
                pendingMutation = mutationSequence
                pendingEvent = BenchmarkEventSequence.injectedEvent
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q && Trace.isEnabled()) {
                    Trace.setCounter(TEXT_MUTATION_COUNTER, mutationSequence)
                    Trace.setCounter(TEXT_MUTATION_EVENT_COUNTER, BenchmarkEventSequence.injectedEvent)
                }
            }
            override fun afterTextChanged(s: Editable?) {}
        })
    }

    override fun onDraw(canvas: Canvas) {
        val tracing = Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q && Trace.isEnabled()
        val tracingThisMutation = tracing && pendingMutation > tracedMutation
        if (tracingThisMutation) {
            Trace.setCounter(DRAWN_MUTATION_COUNTER, pendingMutation)
            Trace.setCounter(DRAWN_EVENT_COUNTER, pendingEvent)
            Trace.beginSection(GLYPH_DRAW_SECTION)
            tracedMutation = pendingMutation
        }
        try {
            super.onDraw(canvas)
        } finally {
            if (tracingThisMutation) Trace.endSection()
        }
    }

    private companion object {
        const val TEXT_MUTATION_COUNTER = "AuraBench text mutation"
        const val TEXT_MUTATION_EVENT_COUNTER = "AuraBench text mutation event"
        const val DRAWN_MUTATION_COUNTER = "AuraBench drawn mutation"
        const val DRAWN_EVENT_COUNTER = "AuraBench drawn event"
        const val GLYPH_DRAW_SECTION = "AuraBench:glyph draw"
    }
}
