package dev.varuntej.aura.alarm

import android.app.Activity
import android.app.KeyguardManager
import android.content.Context
import android.content.Intent
import android.graphics.Color
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.graphics.drawable.StateListDrawable
import android.os.Build
import android.os.Bundle
import android.util.StateSet
import android.util.TypedValue
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.view.WindowManager
import android.widget.Button
import android.widget.FrameLayout
import android.widget.LinearLayout
import android.widget.TextView
import dev.varuntej.aura.MainActivity
import java.time.Instant
import java.time.ZoneId
import java.time.format.DateTimeFormatter

/**
 * The screen the user sees at 3 AM.
 *
 * Built in code rather than from a layout resource on purpose: this runs in a
 * process that may have been started seconds ago for a broadcast, on a locked
 * device, and every file it does not have to inflate is one less thing between
 * the alarm firing and the screen lighting up.
 *
 * Three actions, and the third is the point of the whole feature. Dismiss and
 * Snooze are what any alarm clock does. "I'm up" opens straight into a chat
 * turn, which is what turns a wake-up into a conversation with Buddy instead of
 * a banner the user swipes away.
 */
class AlarmActivity : Activity() {

    private var schedule: AlarmSchedule? = null
    private var rippleView: AlarmRippleView? = null

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        showOverLockScreen()

        schedule = parseSchedule(intent)
        setContentView(buildView())
    }

    /**
     * A second alarm arriving while this one is already on screen.
     *
     * `launchMode="singleInstance"` means the OS reuses this instance rather
     * than creating another, so without rebuilding here the new alarm would ring
     * behind the previous one's time and message, and settling it would ack the
     * wrong reminder.
     */
    override fun onNewIntent(intent: Intent?) {
        super.onNewIntent(intent)
        setIntent(intent)
        val incoming = parseSchedule(intent) ?: return
        schedule = incoming
        setContentView(buildView())
    }

    override fun onResume() {
        super.onResume()
        rippleView?.start()
    }

    override fun onPause() {
        rippleView?.stop()
        super.onPause()
    }

    private fun parseSchedule(from: Intent?): AlarmSchedule? =
        from?.getStringExtra(EXTRA_SCHEDULE)
            ?.let { runCatching { AlarmSchedule.fromJson(org.json.JSONObject(it)) }.getOrNull() }

    /**
     * Deliberately empty: the back button must not silence an alarm.
     *
     * Dismissing has to be a choice the user makes on one of the three buttons,
     * not something a half-asleep reflex can do by accident. The service keeps
     * ringing until one of them is pressed or it gives up on its own.
     */
    @Deprecated("Back must not dismiss an alarm")
    override fun onBackPressed() {
        // Intentionally no super call.
    }

    private fun showOverLockScreen() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O_MR1) {
            setShowWhenLocked(true)
            setTurnScreenOn(true)
            (getSystemService(Context.KEYGUARD_SERVICE) as? KeyguardManager)
                ?.requestDismissKeyguard(this, null)
        } else {
            @Suppress("DEPRECATION")
            window.addFlags(
                WindowManager.LayoutParams.FLAG_SHOW_WHEN_LOCKED or
                    WindowManager.LayoutParams.FLAG_TURN_SCREEN_ON,
            )
        }
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
    }

    // View -----------------------------------------------------------------

    /**
     * The water behind everything, and the three choices in front of it.
     *
     * A [FrameLayout] rather than one column, so the ripples can own the whole
     * screen while the content sits on top of them. Order matters: the ripple
     * view is added FIRST and so is below, which is what lets a stray touch land
     * in the water instead of on Dismiss.
     */
    private fun buildView(): View {
        val root = FrameLayout(this)

        val ripples = AlarmRippleView(this).apply {
            bind(triggerHour())
            layoutParams = FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.MATCH_PARENT,
            )
        }
        rippleView = ripples
        root.addView(ripples)

        val content = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.CENTER
            setPadding(dp(28), dp(48), dp(28), dp(44))
            layoutParams = FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.MATCH_PARENT,
            )
        }

        content.addView(label(clockText(), sizeSp = 64f, color = INK_BRIGHT, light = true))
        content.addView(label("Buddy", sizeSp = 14f, color = INK_FAINT, topDp = 2, tracking = 0.22f))
        content.addView(
            label(
                schedule?.message?.takeIf { it.isNotBlank() } ?: "Time to get up.",
                sizeSp = 20f,
                color = INK_SOFT,
                topDp = 30,
            ),
        )
        val snoozes = schedule?.snoozeCount ?: 0
        if (snoozes > 0) {
            // Shown because it is genuinely useful information at 3 AM: it is the
            // difference between "the alarm just went off" and "you have already
            // pushed this back three times."
            content.addView(
                label(
                    if (snoozes == 1) "Snoozed once already" else "Snoozed $snoozes times already",
                    sizeSp = 14f,
                    color = WARM,
                    topDp = 12,
                ),
            )
        }

        content.addView(spacer(dp(48)))
        content.addView(
            primaryButton("I'm up") { settle(ACTION_IM_UP, openChat = true) },
        )
        content.addView(
            glassButton("Snooze 9 min", topDp = 12) { settle(ACTION_SNOOZE) },
        )
        content.addView(
            ghostButton("Dismiss", topDp = 6) { settle(ACTION_DISMISS) },
        )

        root.addView(content)
        return root
    }

    private fun clockText(): String {
        return runCatching {
            DateTimeFormatter.ofPattern("h:mm a").format(triggerTime())
        }.getOrDefault("")
    }

    /**
     * The hour this alarm was set for, which drives the palette.
     *
     * Not the current clock. An alarm ringing unanswered since 6 AM should still
     * look like 6 AM rather than sliding toward daylight while it is ignored.
     */
    private fun triggerHour(): Float = runCatching {
        val at = triggerTime()
        at.hour + at.minute / 60f
    }.getOrDefault(6f)

    private fun triggerTime() = Instant
        .ofEpochMilli(
            schedule?.let { AlarmSchedule.parseInstant(it.triggerAtIso) }
                ?: System.currentTimeMillis(),
        )
        .atZone(ZoneId.systemDefault())

    // Actions --------------------------------------------------------------

    /**
     * Stop the noise, record what the user chose, and get out of the way.
     *
     * The ack is QUEUED, never sent from here. There may be no network at 3 AM,
     * the Firebase token in the shared keyboard credential store is an hour old
     * and probably expired, and none of that may stand between the user and the
     * alarm stopping. The device has already done the part that matters; the
     * server catches up when the app next runs.
     */
    private fun settle(action: String, openChat: Boolean = false) {
        val current = schedule
        AlarmService.stop(this)

        if (current != null) {
            var nextTriggerIso: String? = null
            if (action == ACTION_SNOOZE) {
                val next = System.currentTimeMillis() + SNOOZE_MS
                nextTriggerIso = Instant.ofEpochMilli(next).toString()
                // Re-armed locally and immediately, so the snooze works with no
                // network at all. The queued ack carries this exact time so a
                // flush hours later records the real snooze rather than one
                // counted from whenever the app happened to open.
                AlarmScheduler.arm(
                    this,
                    current.copy(
                        triggerAtIso = nextTriggerIso,
                        localTimeIso = "",
                        snoozeCount = current.snoozeCount + 1,
                    ),
                )
            }
            AlarmStore.queueAck(this, current.reminderId, action, nextTriggerIso)
        }

        if (openChat && current != null) {
            startActivity(
                Intent(this, MainActivity::class.java)
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP)
                    .putExtra(MainActivity.EXTRA_LAUNCH_ACTION, LAUNCH_ACTION_IM_UP)
                    .putExtra(EXTRA_REMINDER_ID, current.reminderId)
                    .putExtra(EXTRA_MESSAGE, current.message),
            )
        }
        finish()
    }

    // View helpers ---------------------------------------------------------

    private fun dp(value: Int): Int = TypedValue.applyDimension(
        TypedValue.COMPLEX_UNIT_DIP,
        value.toFloat(),
        resources.displayMetrics,
    ).toInt()

    private fun label(
        text: String,
        sizeSp: Float,
        color: String,
        light: Boolean = false,
        topDp: Int = 0,
        tracking: Float = 0f,
    ): TextView = TextView(this).apply {
        this.text = text
        setTextSize(TypedValue.COMPLEX_UNIT_SP, sizeSp)
        setTextColor(Color.parseColor(color))
        gravity = Gravity.CENTER
        // Light rather than bold for the clock. A 64sp bold time is a klaxon in
        // typographic form; thin numerals over moving water read as calm, which
        // is the register the whole screen is trying to hold.
        if (light) typeface = Typeface.create("sans-serif-light", Typeface.NORMAL)
        if (tracking > 0f) letterSpacing = tracking
        layoutParams = LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT,
            ViewGroup.LayoutParams.WRAP_CONTENT,
        ).apply { topMargin = dp(topDp) }
    }

    private fun spacer(height: Int): View = View(this).apply {
        layoutParams = LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, height)
    }

    /** Teal fill on dark ink. The only button that looks like a button. */
    private fun primaryButton(text: String, onClick: () -> Unit): Button =
        button(text, TEAL_DEEP, onClick) {
            setColor(Color.parseColor(TEAL))
        }

    /** Frosted: a hint of fill and a teal edge, so it reads as glass on water. */
    private fun glassButton(text: String, topDp: Int, onClick: () -> Unit): Button =
        button(text, INK_BRIGHT, onClick, topDp) {
            setColor(Color.parseColor(GLASS_FILL))
            setStroke(dp(1), Color.parseColor(GLASS_EDGE))
        }

    /** No fill at all. Dismiss should be findable, never inviting. */
    private fun ghostButton(text: String, topDp: Int, onClick: () -> Unit): Button =
        button(text, INK_FAINT, onClick, topDp) {
            setColor(Color.TRANSPARENT)
        }

    private fun button(
        text: String,
        textColor: String,
        onClick: () -> Unit,
        topDp: Int = 0,
        shape: GradientDrawable.() -> Unit,
    ): Button = Button(this).apply {
        this.text = text
        isAllCaps = false
        setTextSize(TypedValue.COMPLEX_UNIT_SP, 17f)
        setTextColor(Color.parseColor(textColor))
        // A pressed state, which the previous screen had none of. Half-asleep
        // fingers miss, and a button that does not visibly answer a touch gets
        // pressed again and again.
        background = StateListDrawable().apply {
            addState(
                intArrayOf(android.R.attr.state_pressed),
                GradientDrawable().apply {
                    shape()
                    cornerRadius = dp(CORNER_RADIUS_DP).toFloat()
                    setAlpha(PRESSED_ALPHA)
                },
            )
            addState(
                StateSet.WILD_CARD,
                GradientDrawable().apply {
                    shape()
                    cornerRadius = dp(CORNER_RADIUS_DP).toFloat()
                },
            )
        }
        stateListAnimator = null
        setPadding(dp(20), dp(18), dp(20), dp(18))
        setOnClickListener { onClick() }
        layoutParams = LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT,
            ViewGroup.LayoutParams.WRAP_CONTENT,
        ).apply { topMargin = dp(topDp) }
    }

    companion object {
        const val EXTRA_SCHEDULE = "schedule_json"
        const val EXTRA_REMINDER_ID = "aura_alarm_reminder_id"
        const val EXTRA_MESSAGE = "aura_alarm_message"

        /** Matches MainActivity's launch-action whitelist. */
        const val LAUNCH_ACTION_IM_UP = "alarm_im_up"

        const val ACTION_DISMISS = "dismiss"
        const val ACTION_SNOOZE = "snooze"
        const val ACTION_IM_UP = "im_up"

        /** Nine minutes, the interval every mechanical alarm clock has used. */
        const val SNOOZE_MS = 9L * 60L * 1000L

        /**
         * Fully rounded ends. At this radius against a 56dp-tall button the
         * shape stops reading as "a rectangle with soft corners" and starts
         * reading as a pill, which is what keeps the screen from looking like a
         * dialog dropped on top of an animation.
         */
        private const val CORNER_RADIUS_DP = 30

        /** ~72% opacity while held. Visible in a dark room without flashing. */
        private const val PRESSED_ALPHA = 184

        // The house palette. Teal is the app's accent (lib/core/theme/
        // app_colors.dart) and the deep ink is what the ripple view fades to, so
        // the chrome and the water are the same design rather than two.
        private const val TEAL = "#1EC8B0"
        private const val TEAL_DEEP = "#04211D"
        private const val WARM = "#F0B67A"
        private const val INK_BRIGHT = "#F2F6F5"
        private const val INK_SOFT = "#D9E2E1"
        private const val INK_FAINT = "#8A9694"
        private const val GLASS_FILL = "#1AFFFFFF"
        private const val GLASS_EDGE = "#4D1EC8B0"

        fun launchIntent(context: Context, schedule: AlarmSchedule): Intent =
            Intent(context, AlarmActivity::class.java)
                .addFlags(
                    Intent.FLAG_ACTIVITY_NEW_TASK or
                        Intent.FLAG_ACTIVITY_CLEAR_TOP or
                        Intent.FLAG_ACTIVITY_NO_USER_ACTION,
                )
                .putExtra(EXTRA_SCHEDULE, schedule.toJson().toString())
    }
}
