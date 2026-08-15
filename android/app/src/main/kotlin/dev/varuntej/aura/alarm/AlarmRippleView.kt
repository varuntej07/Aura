package dev.varuntej.aura.alarm

import android.annotation.SuppressLint
import android.content.Context
import android.graphics.Canvas
import android.graphics.BlurMaskFilter
import android.graphics.Color
import android.graphics.LinearGradient
import android.graphics.Paint
import android.graphics.Shader
import android.os.Build
import android.os.VibrationEffect
import android.os.Vibrator
import android.os.VibratorManager
import android.util.TypedValue
import android.view.MotionEvent
import android.view.View
import kotlin.math.hypot
import kotlin.math.max
import kotlin.math.min

/**
 * Water, and something dropped into it.
 *
 * The whole point of the alarm screen. Rings spread from the centre in time with
 * the tone that is playing, and touching anywhere throws another stone in. It is
 * the first thing someone sees each morning, so it is drawn rather than
 * decorated: a flat colour with a button on it is a form, and Buddy is not a
 * form.
 *
 * A plain [View] with a [Canvas], deliberately. There is no Compose dependency
 * in this module and adding one would put a compiler plugin and a runtime on the
 * path of a process that starts, cold, on a locked phone, seconds before it has
 * to show something. Concentric circles are two dozen draw calls; that is not
 * where a frame budget goes.
 *
 * TIMING
 * ------
 * Ripples are emitted on the tone's beat, and the beat is derived rather than
 * signalled: [AlarmService] records the instant audio started and which slug is
 * playing, and this reads that anchor and computes `elapsed / beatPeriod`. No
 * IPC, no callback to miss, and it re-synchronises for free if this view is
 * recreated mid-ring. Every bundled clip loops on an exact multiple of its beat,
 * so one anchor holds for the full ten minutes.
 *
 * A tone with no beat this build can know (a sound picked from the user's own
 * device, or any fallback) emits on a slow ambient period instead. Confidently
 * pulsing on a beat that is not there looks worse than not trying.
 */
class AlarmRippleView(context: Context) : View(context) {

    // Palette ---------------------------------------------------------------

    /**
     * One moment of the day. Only the water and the sky change with the hour;
     * text stays put, because legibility at 3 AM is not a place for a gradient.
     */
    private class Palette(
        val skyTop: Int,
        val skyBottom: Int,
        val ripples: IntArray,
    )

    private var palette = NIGHT
    private var sky: LinearGradient? = null

    // Rings -----------------------------------------------------------------

    private class Ring(
        val spawnedAt: Long,
        val cx: Float,
        val cy: Float,
        val color: Int,
        /** Touch rings are brighter and travel further than the beat's own. */
        val emphasis: Float,
    )

    private val rings = ArrayDeque<Ring>()
    private var nextColor = 0

    private val ringPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE
        strokeCap = Paint.Cap.ROUND
    }
    private val glowMasks: Array<BlurMaskFilter?> = arrayOf(
        BlurMaskFilter(dp(14f), BlurMaskFilter.Blur.NORMAL),
        BlurMaskFilter(dp(7f), BlurMaskFilter.Blur.NORMAL),
        null,
    )
    private val skyPaint = Paint()

    // Beat ------------------------------------------------------------------

    private var beatPeriodMs = AlarmTones.AMBIENT_BEAT_MS
    private var anchorMs = 0L
    private var lastBeat = -1L
    private var running = false

    /** Set on the first frame and used until the service publishes a real one. */
    private var ambientAnchorMs = 0L
    private var lastAnchorPoll = 0L

    private val vibrator: Vibrator? by lazy {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            (context.getSystemService(Context.VIBRATOR_MANAGER_SERVICE) as? VibratorManager)
                ?.defaultVibrator
        } else {
            @Suppress("DEPRECATION")
            context.getSystemService(Context.VIBRATOR_SERVICE) as? Vibrator
        }
    }

    init {
        // Every ring is an antialiased stroke, and there may be two dozen on
        // screen at once. This is exactly the case a hardware layer exists for.
        setLayerType(LAYER_TYPE_HARDWARE, null)
        isClickable = false
        isFocusable = false
    }

    /**
     * Point this at a specific alarm.
     *
     * [triggerHour] is the hour the alarm was set for rather than the current
     * clock, so an alarm that has been ringing unanswered since 6 AM still looks
     * like 6 AM instead of drifting into daylight while the sleeper ignores it.
     */
    fun bind(triggerHour: Float) {
        palette = paletteAt(triggerHour)
        sky = null
        invalidate()
    }

    // Lifecycle -------------------------------------------------------------

    override fun onAttachedToWindow() {
        super.onAttachedToWindow()
        start()
    }

    override fun onDetachedFromWindow() {
        // Nothing is visible, so nothing should be drawing. Without this the
        // frame callback keeps the CPU busy behind the lock screen for as long
        // as the activity is alive.
        stop()
        super.onDetachedFromWindow()
    }

    /** Resume frame callbacks when the alarm Activity becomes visible. */
    fun start() {
        if (running) return
        running = true
        readBeat()
        postInvalidateOnAnimation()
    }

    /** Stop frame callbacks while the Activity is paused or detached. */
    fun stop() {
        running = false
    }

    override fun onSizeChanged(w: Int, h: Int, oldw: Int, oldh: Int) {
        super.onSizeChanged(w, h, oldw, oldh)
        sky = null
    }

    /**
     * Re-read the ring anchor the service wrote.
     *
     * Called on attach and again on the first beat, because the activity and the
     * service start moments apart in either order: a full-screen intent can put
     * this on screen before [AlarmService] has finished preparing its player.
     */
    private fun readBeat() {
        val context = context.applicationContext
        anchorMs = AlarmStore.ringStartedAt(context)
        beatPeriodMs = AlarmTones.beatPeriodMs(AlarmStore.ringTone(context))
    }

    // Drawing ---------------------------------------------------------------

    override fun onDraw(canvas: Canvas) {
        val now = System.currentTimeMillis()
        drawSky(canvas)
        emitDueRings(now)
        drawRings(canvas, now)
        if (running) postInvalidateOnAnimation()
    }

    private fun drawSky(canvas: Canvas) {
        val shader = sky ?: LinearGradient(
            0f, 0f, 0f, height.toFloat(),
            palette.skyTop, palette.skyBottom,
            Shader.TileMode.CLAMP,
        ).also { sky = it }
        skyPaint.shader = shader
        canvas.drawRect(0f, 0f, width.toFloat(), height.toFloat(), skyPaint)
    }

    private fun emitDueRings(now: Long) {
        // The activity and the service start moments apart in either order, and
        // a full-screen intent regularly puts this on screen before the player
        // is prepared. Poll for the anchor rather than freezing until it lands.
        if (anchorMs == 0L && now - lastAnchorPoll > ANCHOR_POLL_MS) {
            lastAnchorPoll = now
            readBeat()
        }

        val anchor: Long
        val period: Long
        if (anchorMs != 0L) {
            anchor = anchorMs
            period = beatPeriodMs
        } else {
            // Water before the sound, and water after it stops. The screen is
            // never still, because a frozen ripple reads as a crashed app.
            if (ambientAnchorMs == 0L) ambientAnchorMs = now
            anchor = ambientAnchorMs
            period = AlarmTones.AMBIENT_BEAT_MS
        }

        val beat = (now - anchor) / period
        if (beat == lastBeat) return
        // One ring per frame at most, never a catch-up burst. After a stall (a
        // collection, the screen waking) the beat index jumps by dozens, and
        // replaying them would fire a wall of rings for sound already past.
        lastBeat = beat
        spawnCentreRing(now)
    }

    private fun spawnCentreRing(now: Long) {
        add(Ring(now, width / 2f, height * CENTRE_BIAS, nextRippleColor(), emphasis = 1f))
    }

    private fun add(ring: Ring) {
        rings.addLast(ring)
        // A hard ceiling rather than a soft one. Rings expire on their own, but
        // a finger dragged across the screen can outrun expiry, and an unbounded
        // list would turn into dropped frames on the one screen that must not.
        while (rings.size > MAX_RINGS) rings.removeFirst()
    }

    private fun nextRippleColor(): Int =
        palette.ripples[nextColor++ % palette.ripples.size]

    private fun drawRings(canvas: Canvas, now: Long) {
        if (rings.isEmpty()) return
        val reach = hypot(width / 2f, height / 2f) * 1.15f
        val iterator = rings.iterator()
        while (iterator.hasNext()) {
            val ring = iterator.next()
            val progress = (now - ring.spawnedAt).toFloat() / RING_LIFE_MS
            if (progress >= 1f) {
                iterator.remove()
                continue
            }
            drawRing(canvas, ring, progress, reach)
        }
    }

    private fun drawRing(canvas: Canvas, ring: Ring, progress: Float, reach: Float) {
        val radius = reach * ring.emphasis * easeOut(progress)
        if (radius <= 1f) return

        val alpha = alphaAt(progress) * ring.emphasis
        if (alpha <= 0.004f) return

        val stroke = lerp(dp(RING_START_WIDTH_DP), dp(RING_END_WIDTH_DP), progress)

        canvas.save()
        // Squashed vertically so the rings read as a surface seen at an angle
        // rather than as circles on a wall. This is the difference between
        // "water" and "concentric shapes".
        canvas.scale(1f, SURFACE_TILT, ring.cx, ring.cy)
        // Three passes, widest and faintest first. A cheap bloom that works
        // identically on every device: BlurMaskFilter is the obvious tool and is
        // the one thing here that is not reliably hardware-accelerated.
        for ((index, pass) in GLOW_PASSES.withIndex()) {
            val (widthScale, alphaScale) = pass
            ringPaint.color = ring.color
            ringPaint.alpha = (alpha * alphaScale * 255f).toInt().coerceIn(0, 255)
            ringPaint.strokeWidth = stroke * widthScale
            ringPaint.maskFilter = glowMasks[index]
            canvas.drawCircle(ring.cx, ring.cy, radius, ringPaint)
        }
        ringPaint.maskFilter = null
        canvas.restore()
    }

    /** Fast in, long fade: how a real ripple loses its edge as it travels. */
    private fun alphaAt(progress: Float): Float = when {
        progress < 0.10f -> lerp(0f, PEAK_ALPHA, progress / 0.10f)
        progress < 0.42f -> lerp(PEAK_ALPHA, SETTLED_ALPHA, (progress - 0.10f) / 0.32f)
        else -> lerp(SETTLED_ALPHA, 0f, (progress - 0.42f) / 0.58f)
    }

    // Touch -----------------------------------------------------------------

    /**
     * Throw a stone in.
     *
     * Consumes nothing it should not: this view sits BEHIND the clock and the
     * buttons in a FrameLayout, so a press on "Dismiss" is the button's and
     * never reaches here. A half-asleep poke at the water cannot silence an
     * alarm by accident.
     */
    @SuppressLint("ClickableViewAccessibility")
    override fun onTouchEvent(event: MotionEvent): Boolean {
        if (event.actionMasked != MotionEvent.ACTION_DOWN) return false
        add(Ring(System.currentTimeMillis(), event.x, event.y, nextRippleColor(), TOUCH_EMPHASIS))
        tick()
        return true
    }

    /**
     * A single short tap under the finger, so the water answers by feel too.
     *
     * Not USAGE_ALARM: this is a UI confirmation, and tagging it as an alarm
     * would exempt it from the user's own vibration settings for no reason. The
     * alarm's real buzz is [AlarmService]'s and is entirely separate.
     */
    private fun tick() {
        val device = vibrator ?: return
        if (!device.hasVibrator()) return
        runCatching {
            device.vibrate(VibrationEffect.createOneShot(TICK_MS, TICK_AMPLITUDE))
        }
    }

    // Helpers ---------------------------------------------------------------

    private fun dp(value: Float): Float = TypedValue.applyDimension(
        TypedValue.COMPLEX_UNIT_DIP, value, resources.displayMetrics,
    )

    private fun easeOut(t: Float): Float {
        val inverted = 1f - t
        return 1f - inverted * inverted * inverted
    }

    private fun lerp(from: Float, to: Float, t: Float): Float =
        from + (to - from) * t.coerceIn(0f, 1f)

    companion object {

        /**
         * Rings start above centre, where the clock is not.
         *
         * Dead centre puts the brightest part of every ripple directly behind
         * the message text, which is the one thing on screen that has to be
         * readable.
         */
        private const val CENTRE_BIAS = 0.38f

        private const val RING_LIFE_MS = 4200f
        private const val MAX_RINGS = 24

        private const val RING_START_WIDTH_DP = 22f
        private const val RING_END_WIDTH_DP = 1.5f

        private const val PEAK_ALPHA = 0.30f
        private const val SETTLED_ALPHA = 0.18f

        /** Touch rings travel further and burn brighter than the beat's. */
        private const val TOUCH_EMPHASIS = 1.35f

        /** Vertical squash. 1.0 is a wall; this is a pond. */
        private const val SURFACE_TILT = 0.86f

        /** (stroke width multiplier, alpha multiplier), widest and faintest first. */
        private val GLOW_PASSES = arrayOf(
            3.2f to 0.22f,
            1.7f to 0.40f,
            1.0f to 1.00f,
        )

        private const val TICK_MS = 12L
        private const val TICK_AMPLITUDE = 90

        /** How often to look for the service's ring anchor before it exists. */
        private const val ANCHOR_POLL_MS = 250L

        // Colours are the app icon and the aura-web ripple palette, nothing new.
        // Teal #1EC8B0 and bright #34E3CB are the house accent; violet #966EF5,
        // blue #5A96FF and cyan #6EE1EB come from the web client's wave; warm
        // #E89B5A and #F0B67A are the amber the icon fades into.
        private val NIGHT = Palette(
            skyTop = Color.parseColor("#080A0F"),
            skyBottom = Color.parseColor("#0E1418"),
            ripples = intArrayOf(
                Color.parseColor("#6EE1EB"),
                Color.parseColor("#966EF5"),
                Color.parseColor("#5A96FF"),
            ),
        )

        private val DAWN = Palette(
            skyTop = Color.parseColor("#0B1014"),
            skyBottom = Color.parseColor("#12181C"),
            ripples = intArrayOf(
                Color.parseColor("#1EC8B0"),
                Color.parseColor("#34E3CB"),
                Color.parseColor("#E89B5A"),
            ),
        )

        private val DAY = Palette(
            skyTop = Color.parseColor("#12100E"),
            skyBottom = Color.parseColor("#1A1512"),
            ripples = intArrayOf(
                Color.parseColor("#E89B5A"),
                Color.parseColor("#F0B67A"),
                Color.parseColor("#1EC8B0"),
            ),
        )

        /**
         * The palette for an hour of the day, ramped rather than switched.
         *
         * All three stops are dark. This screen only ever appears at full
         * brightness in a room whose lights are off, so "day" here means warm
         * rather than bright; a cream 3 AM alarm is a hostile act.
         *
         * Evening walks back down through dawn to night, so a 10 PM alarm looks
         * like night rather than like noon.
         */
        private fun paletteAt(hour: Float): Palette {
            val h = hour.coerceIn(0f, 24f)
            return when {
                h < 5f -> NIGHT
                h < 8f -> blend(NIGHT, DAWN, (h - 5f) / 3f)
                h < 11f -> blend(DAWN, DAY, (h - 8f) / 3f)
                h < 18f -> DAY
                h < 21f -> blend(DAY, DAWN, (h - 18f) / 3f)
                h < 23f -> blend(DAWN, NIGHT, (h - 21f) / 2f)
                else -> NIGHT
            }
        }

        private fun blend(from: Palette, to: Palette, t: Float): Palette {
            val clamped = t.coerceIn(0f, 1f)
            return Palette(
                skyTop = mix(from.skyTop, to.skyTop, clamped),
                skyBottom = mix(from.skyBottom, to.skyBottom, clamped),
                ripples = IntArray(from.ripples.size) { index ->
                    mix(from.ripples[index], to.ripples[index], clamped)
                },
            )
        }

        private fun mix(from: Int, to: Int, t: Float): Int = Color.rgb(
            channel(Color.red(from), Color.red(to), t),
            channel(Color.green(from), Color.green(to), t),
            channel(Color.blue(from), Color.blue(to), t),
        )

        private fun channel(from: Int, to: Int, t: Float): Int =
            max(0, min(255, (from + (to - from) * t).toInt()))
    }
}
