package dev.varuntej.aura.keyboard.input

import android.content.Context
import android.view.MotionEvent
import android.view.ViewConfiguration
import android.widget.ScrollView

/**
 * A vertical [ScrollView] that also reports clean horizontal swipes.
 *
 * The emoji grid's cells are clickable, so they consume ACTION_DOWN and an OnTouchListener on
 * the scroller would never see the drag. Interception is the only hook that works here, and it
 * is the same mechanism ScrollView itself uses to claim vertical drags from its children.
 *
 * Deliberately conservative: the gesture is claimed only once horizontal travel passes the
 * touch slop AND clearly dominates vertical travel, so a tap still inserts an emoji and a
 * vertical drag still scrolls. Once claimed, the gesture is swallowed to the end, so no emoji
 * is inserted by the finger lifting mid-swipe.
 */
class HorizontalSwipeScrollView(context: Context) : ScrollView(context) {

    /** Finger moved right-to-left: the caller advances to the next page. */
    var onSwipeLeft: (() -> Unit)? = null

    /** Finger moved left-to-right: the caller goes back a page. */
    var onSwipeRight: (() -> Unit)? = null

    private val touchSlop = ViewConfiguration.get(context).scaledTouchSlop
    private var downX = 0f
    private var downY = 0f
    private var claimed = false

    /** How far horizontal travel must beat vertical travel before this reads as a sideways
     *  swipe rather than a slightly untidy vertical scroll. */
    private val horizontalDominance = 1.5f

    override fun onInterceptTouchEvent(event: MotionEvent): Boolean {
        when (event.actionMasked) {
            MotionEvent.ACTION_DOWN -> {
                downX = event.x
                downY = event.y
                claimed = false
            }
            MotionEvent.ACTION_MOVE -> {
                val dx = event.x - downX
                val dy = event.y - downY
                if (!claimed && kotlin.math.abs(dx) > touchSlop &&
                    kotlin.math.abs(dx) > kotlin.math.abs(dy) * horizontalDominance
                ) {
                    claimed = true
                    return true
                }
            }
        }
        // Not clearly horizontal: let ScrollView decide as it always has.
        return if (claimed) true else super.onInterceptTouchEvent(event)
    }

    override fun onTouchEvent(event: MotionEvent): Boolean {
        if (!claimed) return super.onTouchEvent(event)
        when (event.actionMasked) {
            MotionEvent.ACTION_UP -> {
                val dx = event.x - downX
                if (dx < 0) onSwipeLeft?.invoke() else onSwipeRight?.invoke()
                claimed = false
            }
            MotionEvent.ACTION_CANCEL -> claimed = false
        }
        // Swallowed for the rest of the gesture, so the lift never lands on a child.
        return true
    }
}
