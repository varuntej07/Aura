package dev.varuntej.aura.keyboard.input

import android.annotation.SuppressLint
import android.content.Context
import android.os.Handler
import android.os.Looper
import android.view.Gravity
import android.view.HapticFeedbackConstants
import android.view.MotionEvent
import android.view.View
import android.view.ViewConfiguration
import android.widget.LinearLayout
import android.widget.PopupWindow
import android.widget.TextView
import androidx.core.content.ContextCompat
import dev.varuntej.aura.R

/**
 * Touch handling for a single character key: a simple pressed-state highlight (no hover bubble, no
 * scale, no per-key haptic) and a long-press accent/alternate popup. Thin and Android-coupled; the
 * alternates come from the pure [KeyPopupOptions].
 *
 * A normal tap fires [onTap] (the usual key commit). A long press shows the alternates; sliding
 * the finger selects one and lifting commits it via [onAlternate]; lifting off the popup commits
 * nothing. Every popup is dismissed on up/cancel, so nothing is left on screen.
 */
@SuppressLint("ClickableViewAccessibility")
class KeyTouchHandler(
    private val context: Context,
    private val keyView: TextView,
    private val alternates: List<String>,
    private val isShifted: () -> Boolean,
    private val onTap: () -> Unit,
    private val onAlternate: (String) -> Unit,
) : View.OnTouchListener {

    private val handler = Handler(Looper.getMainLooper())
    private val longPressTimeout = ViewConfiguration.getLongPressTimeout().toLong()
    private var longPressRunnable: Runnable? = null

    private var altPopup: PopupWindow? = null
    private var altChips: List<TextView> = emptyList()
    private var selected = -1

    // True once the finger has slid off the key (beyond slop) during this press, so the lift
    // commits nothing (a real keyboard cancels the tap when you drag away from the key).
    private var movedOffKey = false
    private val touchSlop = ViewConfiguration.get(context).scaledTouchSlop

    private val density = context.resources.displayMetrics.density
    private fun dp(value: Int) = (value * density).toInt()

    // Reused scratch for the alternates hit-test, so ACTION_MOVE allocates nothing per event.
    private val chipLocation = IntArray(2)

    init {
        // The IME view can be torn down (field change, app switch, keyboard hide) mid-gesture
        // without delivering ACTION_CANCEL to this per-key listener. When that happens the key view
        // detaches, so cancel the pending long-press and dismiss any popup here, otherwise the
        // delayed Runnable keeps the handler -> key view -> service chain alive (leak) and can fire
        // showAsDropDown on a window that is already gone.
        keyView.addOnAttachStateChangeListener(object : View.OnAttachStateChangeListener {
            override fun onViewAttachedToWindow(v: View) {}
            override fun onViewDetachedFromWindow(v: View) = cancel()
        })
    }

    override fun onTouch(v: View, event: MotionEvent): Boolean {
        when (event.actionMasked) {
            MotionEvent.ACTION_DOWN -> onDown()
            MotionEvent.ACTION_MOVE -> onMove(event)
            MotionEvent.ACTION_UP -> onUp()
            MotionEvent.ACTION_CANCEL -> cancel()
        }
        return true
    }

    private fun onDown() {
        movedOffKey = false
        // Simple press feedback: the pressed-state background only (no hover bubble, no scale, no
        // haptic), so a keystroke does no per-key window or animation work.
        keyView.isPressed = true
        if (alternates.isNotEmpty()) {
            val runnable = Runnable { showAltPopup() }
            longPressRunnable = runnable
            handler.postDelayed(runnable, longPressTimeout)
        }
    }

    private fun onMove(event: MotionEvent) {
        // Once the alternates popup is open, a move selects among its chips.
        if (altPopup != null) {
            onAltMove(event.rawX)
            return
        }
        // Before the popup opens, sliding the finger off the key cancels both the pending long-press
        // and the eventual tap, so dragging away to another key never commits this one.
        if (!movedOffKey && isOutsideKey(event)) {
            movedOffKey = true
            cancelLongPress()
            clearPressed()
        }
    }

    /** Whether [event] (coordinates relative to [keyView]) is outside the key plus touch slop. */
    private fun isOutsideKey(event: MotionEvent): Boolean =
        event.x < -touchSlop || event.y < -touchSlop ||
            event.x > keyView.width + touchSlop || event.y > keyView.height + touchSlop

    private fun onAltMove(rawX: Float) {
        val chips = altChips
        if (chips.isEmpty()) return
        var index = -1
        for (i in chips.indices) {
            val chip = chips[i]
            chip.getLocationOnScreen(chipLocation)
            if (rawX >= chipLocation[0] && rawX < chipLocation[0] + chip.width) {
                index = i
                break
            }
        }
        if (index != selected) {
            selected = index
            highlight()
        }
    }

    private fun onUp() {
        cancelLongPress()
        clearPressed()
        val popupWasOpen = altPopup != null
        val chosen = if (popupWasOpen) alternates.getOrNull(selected) else null
        // Commit the keystroke BEFORE any popup teardown: dismissing a PopupWindow is a
        // WindowManager transaction, and doing it first would make every character wait behind it.
        when {
            chosen != null -> onAlternate(chosen)
            popupWasOpen -> {} // popup was open but lifted off every chip: commit nothing.
            movedOffKey -> {}  // finger slid off the key before lifting: commit nothing.
            else -> onTap()
        }
        dismissAltPopup()
    }

    private fun cancel() {
        cancelLongPress()
        clearPressed()
        dismissAltPopup()
    }

    private fun clearPressed() {
        keyView.isPressed = false
    }

    private fun cancelLongPress() {
        longPressRunnable?.let { handler.removeCallbacks(it) }
        longPressRunnable = null
    }

    private fun cased(text: String): String = if (isShifted()) text.uppercase() else text

    private fun showAltPopup() {
        keyView.performHapticFeedback(HapticFeedbackConstants.LONG_PRESS)
        val row = LinearLayout(context).apply {
            orientation = LinearLayout.HORIZONTAL
            setBackgroundResource(R.drawable.buddy_kb_popup_bg)
            val pad = dp(4)
            setPadding(pad, pad, pad, pad)
        }
        val chipWidth = dp(40)
        val chipHeight = dp(46)
        val chips = alternates.map { alternate ->
            TextView(context).apply {
                text = cased(alternate)
                gravity = Gravity.CENTER
                textSize = 22f
                setTextColor(ContextCompat.getColor(context, R.color.buddy_kb_key_text))
                layoutParams = LinearLayout.LayoutParams(chipWidth, chipHeight)
                    .apply { setMargins(dp(2), 0, dp(2), 0) }
            }
        }
        chips.forEach { row.addView(it) }
        altChips = chips
        // Nothing pre-selected: a long-press followed by a straight lift (no slide onto a chip)
        // commits nothing, matching the documented behavior. The user slides to pick an alternate.
        selected = -1
        highlight()

        row.measure(View.MeasureSpec.UNSPECIFIED, View.MeasureSpec.UNSPECIFIED)
        val popupWidth = row.measuredWidth
        val popupHeight = chipHeight + dp(8)
        val popup = PopupWindow(row, popupWidth, popupHeight).apply {
            isClippingEnabled = false
            elevation = dp(8).toFloat()
        }
        altPopup = popup
        val xOffset = (keyView.width - popupWidth) / 2
        val yOffset = -(keyView.height + popupHeight + dp(4))
        // The long-press timer can fire after the key view detaches (field change mid-hold);
        // showAsDropDown on a gone window throws BadTokenException and would crash the IME. Guard.
        if (!keyView.isAttachedToWindow) {
            dismissAltPopup()
            return
        }
        try {
            popup.showAsDropDown(keyView, xOffset, yOffset, Gravity.START)
        } catch (_: Throwable) {
            dismissAltPopup()
        }
    }

    private fun highlight() {
        altChips.forEachIndexed { i, chip ->
            if (i == selected) {
                chip.setBackgroundResource(R.drawable.buddy_kb_chip_bg)
                chip.setTextColor(ContextCompat.getColor(context, R.color.buddy_kb_accent_text))
            } else {
                chip.background = null
                chip.setTextColor(ContextCompat.getColor(context, R.color.buddy_kb_key_text))
            }
        }
    }

    private fun dismissAltPopup() {
        altPopup?.dismiss()
        altPopup = null
        altChips = emptyList()
        selected = -1
    }

}
