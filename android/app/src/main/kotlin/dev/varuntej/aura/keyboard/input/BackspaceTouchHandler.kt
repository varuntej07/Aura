package dev.varuntej.aura.keyboard.input

import android.annotation.SuppressLint
import android.os.Handler
import android.os.Looper
import android.view.HapticFeedbackConstants
import android.view.MotionEvent
import android.view.View

/**
 * Touch handling for the backspace key: a tap deletes one character, holding auto-repeats with
 * acceleration (the fast-clear gesture), and swiping left deletes whole words. Thin and
 * Android-coupled; the accelerating delays come from the pure [BackspaceRepeat], and the actual
 * deletes are delegated so they stay composer-aware in the IME.
 */
@SuppressLint("ClickableViewAccessibility")
class BackspaceTouchHandler(
    private val backspaceView: View,
    private val onDeleteChar: () -> Unit,
    private val onDeleteWord: () -> Unit,
) : View.OnTouchListener {

    private val handler = Handler(Looper.getMainLooper())
    private var repeatRunnable: Runnable? = null
    private var tick = 0

    private var startX = 0f
    private var swiping = false
    private var wordsDeleted = 0

    private val density = backspaceView.resources.displayMetrics.density
    private val swipeThresholdPx = 24 * density
    private val pxPerWord = 40 * density

    init {
        // The repeat Runnable reschedules itself indefinitely and is otherwise only stopped on
        // ACTION_UP/CANCEL. If the IME view tears down mid-hold (field change, app switch) without
        // delivering CANCEL to this listener, the un-cancelled loop would leak the handler -> view
        // chain and keep deleting from whatever field gains focus next. Stop it on detach.
        backspaceView.addOnAttachStateChangeListener(object : View.OnAttachStateChangeListener {
            override fun onViewAttachedToWindow(v: View) {}
            override fun onViewDetachedFromWindow(v: View) = cancelRepeat()
        })
    }

    override fun onTouch(v: View, event: MotionEvent): Boolean {
        when (event.actionMasked) {
            MotionEvent.ACTION_DOWN -> onDown(event.rawX)
            MotionEvent.ACTION_MOVE -> onMove(event.rawX)
            MotionEvent.ACTION_UP, MotionEvent.ACTION_CANCEL -> onUp()
        }
        return true
    }

    private fun onDown(rawX: Float) {
        startX = rawX
        swiping = false
        wordsDeleted = 0
        tick = 0
        backspaceView.animate().scaleX(0.92f).scaleY(0.92f).setDuration(40).start()
        backspaceView.performHapticFeedback(HapticFeedbackConstants.KEYBOARD_TAP)
        onDeleteChar() // immediate one-character delete (the tap)
        scheduleRepeat(BackspaceRepeat.INITIAL_DELAY_MS)
    }

    private fun onMove(rawX: Float) {
        val dragLeft = startX - rawX
        if (!swiping && dragLeft > swipeThresholdPx) {
            // A left swipe takes over: stop char auto-repeat and switch to deleting words.
            swiping = true
            cancelRepeat()
        }
        if (swiping) {
            val target = (dragLeft / pxPerWord).toInt()
            while (wordsDeleted < target) {
                onDeleteWord()
                wordsDeleted++
            }
        }
    }

    private fun onUp() {
        cancelRepeat()
        backspaceView.animate().scaleX(1f).scaleY(1f).setDuration(70).start()
    }

    private fun scheduleRepeat(delay: Long) {
        val runnable = Runnable {
            onDeleteChar()
            scheduleRepeat(BackspaceRepeat.delayForTick(tick++))
        }
        repeatRunnable = runnable
        handler.postDelayed(runnable, delay)
    }

    private fun cancelRepeat() {
        repeatRunnable?.let { handler.removeCallbacks(it) }
        repeatRunnable = null
    }
}
