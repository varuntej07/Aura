package dev.varuntej.aura.keyboard

import android.animation.ValueAnimator
import android.content.Context
import android.graphics.Canvas
import android.graphics.Paint
import android.graphics.RectF
import android.view.View
import android.view.animation.LinearInterpolator
import kotlin.math.sin

/**
 * A compact animated equalizer for the in-keyboard voice panel: a handful of rounded bars that
 * breathe with a phase offset so the cluster reads as a live audio meter. Three energy levels
 * drive amplitude + speed, so the motion itself shows the session state without any text:
 *
 *  - SPEAKING:  tall, fast bars (Buddy is talking)
 *  - LISTENING: gentle, slow bars (waiting on the user)
 *  - IDLE:      a flat resting line (connecting / between turns)
 *
 * Self-managing: the driver starts on attach and is cancelled on detach (and via [release]),
 * so the view can be added to / removed from the panel without leaking an animator into the
 * IME process. It owns no LiveKit state; the panel just calls [setEnergy].
 */
class VoiceWaveformView(context: Context) : View(context) {

    enum class Energy { IDLE, LISTENING, SPEAKING }

    private val barPaint = Paint(Paint.ANTI_ALIAS_FLAG)
    private val barRect = RectF()
    private val barCount = 5
    private var phase = 0f
    private var energy = Energy.IDLE

    // One infinite phase sweep drives every bar; per-bar offsets in [onDraw] keep them out of
    // sync so the meter looks organic rather than a single pumping block.
    private val driver = ValueAnimator.ofFloat(0f, (2.0 * Math.PI).toFloat()).apply {
        duration = 1400L
        repeatCount = ValueAnimator.INFINITE
        interpolator = LinearInterpolator()
        addUpdateListener {
            phase = it.animatedValue as Float
            invalidate()
        }
    }

    fun setBarColor(color: Int) {
        barPaint.color = color
        invalidate()
    }

    fun setEnergy(value: Energy) {
        if (energy == value) return
        energy = value
        // A faster sweep while Buddy speaks so the meter visibly quickens.
        driver.duration = when (value) {
            Energy.SPEAKING -> 520L
            Energy.LISTENING -> 900L
            Energy.IDLE -> 1400L
        }
        invalidate()
    }

    /** Stop the driver explicitly (when the panel closes without detaching the view yet). */
    fun release() {
        driver.cancel()
    }

    override fun onAttachedToWindow() {
        super.onAttachedToWindow()
        if (!driver.isStarted) driver.start()
    }

    override fun onDetachedFromWindow() {
        driver.cancel()
        super.onDetachedFromWindow()
    }

    override fun onDraw(canvas: Canvas) {
        val n = barCount
        val w = width.toFloat()
        val h = height.toFloat()
        if (w <= 0f || h <= 0f) return

        // n bars with (n - 1) equal gaps, no outer margin: unit = w / (2n - 1).
        val unit = w / (2f * n - 1f)
        val radius = unit / 2f
        val minFrac = 0.16f
        val amplitude = when (energy) {
            Energy.SPEAKING -> 0.84f
            Energy.LISTENING -> 0.44f
            Energy.IDLE -> 0f
        }
        for (i in 0 until n) {
            val offset = i * 0.9f
            val swing = (sin(phase + offset) + 1f) / 2f // 0..1
            val frac = (minFrac + amplitude * swing).coerceIn(0.06f, 1f)
            val barHeight = h * frac
            val left = i * 2f * unit
            val top = (h - barHeight) / 2f
            barRect.set(left, top, left + unit, top + barHeight)
            canvas.drawRoundRect(barRect, radius, radius, barPaint)
        }
    }
}
