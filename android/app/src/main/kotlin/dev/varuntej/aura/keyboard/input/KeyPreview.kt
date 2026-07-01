package dev.varuntej.aura.keyboard.input

import android.content.Context
import android.os.Handler
import android.os.Looper
import android.view.Gravity
import android.view.View
import android.widget.PopupWindow
import android.widget.TextView
import androidx.core.content.ContextCompat
import dev.varuntej.aura.R

/**
 * A SINGLE key-preview bubble shared by every key.
 *
 * The previous design gave each key its own [PopupWindow] and added + removed a window on every
 * press, two WindowManager transactions per keystroke and the main typing-lag source on a
 * View-based keyboard. This reuses one popup instead: the first key shows it, each following key
 * just repositions it ([PopupWindow.update], no add/remove), and it is hidden only once typing
 * pauses ([hideSoon]). So a fast typing burst costs ONE window add and ONE remove total, however
 * many keys it spans.
 *
 * Owned by the IME for the lifetime of the input view and passed to every [KeyTouchHandler].
 */
class KeyPreview(private val context: Context) {

    private val handler = Handler(Looper.getMainLooper())
    private val density = context.resources.displayMetrics.density
    private fun dp(value: Int) = (value * density).toInt()

    private val width = dp(46)
    private val height = dp(52)

    private val bubble: TextView by lazy {
        TextView(context).apply {
            gravity = Gravity.CENTER
            textSize = 24f
            setTextColor(ContextCompat.getColor(context, R.color.buddy_kb_key_text))
            setBackgroundResource(R.drawable.buddy_kb_popup_bg)
        }
    }

    private val popup: PopupWindow by lazy {
        PopupWindow(bubble, width, height).apply {
            isClippingEnabled = false
            elevation = dp(6).toFloat()
        }
    }

    private val hideRunnable = Runnable { dismiss() }

    /**
     * Show the bubble over [anchor] carrying [text]. If it is already up (a previous key in the
     * same burst), just move + retext it with no window add/remove. Cancels any pending idle-hide.
     * Anchoring to a detached key throws BadTokenException, so guard + swallow defensively.
     */
    fun show(anchor: View, text: CharSequence) {
        if (!anchor.isAttachedToWindow) return
        handler.removeCallbacks(hideRunnable)
        bubble.text = text
        val xOffset = (anchor.width - width) / 2
        val yOffset = -(anchor.height + height + dp(4))
        try {
            if (popup.isShowing) {
                popup.update(anchor, xOffset, yOffset, width, height)
            } else {
                popup.showAsDropDown(anchor, xOffset, yOffset, Gravity.START)
            }
        } catch (_: Throwable) {
            dismiss()
        }
    }

    /**
     * Hide after a short idle. A continuous typing burst keeps ONE popup alive (each key just
     * repositions it) and only the trailing pause tears it down, so the bubble never flickers
     * add/remove between consecutive keystrokes.
     */
    fun hideSoon() {
        handler.removeCallbacks(hideRunnable)
        handler.postDelayed(hideRunnable, IDLE_HIDE_MS)
    }

    /** Hide now: a finger slid off a key, a gesture was cancelled, or the keyboard is going away. */
    fun dismiss() {
        handler.removeCallbacks(hideRunnable)
        try {
            popup.dismiss()
        } catch (_: Throwable) {
        }
    }

    companion object {
        // Long enough to bridge the gap between two keystrokes in a fast burst, short enough that
        // the bubble is gone almost as soon as you stop typing.
        private const val IDLE_HIDE_MS = 150L
    }
}
