package dev.varuntej.aura.alarm

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.media.AudioAttributes
import android.media.MediaPlayer
import android.media.RingtoneManager
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.os.PowerManager
import android.os.VibrationAttributes
import android.os.VibrationEffect
import android.os.Vibrator
import android.os.VibratorManager
import android.util.Log
import dev.varuntej.aura.R

/**
 * The thing that actually wakes someone up.
 *
 * A foreground service rather than work inside the receiver, because a receiver
 * gets a few seconds and an alarm has to hold the device awake and keep making
 * noise for minutes. It owns three things at once: the wake lock, the audio, and
 * the notification carrying the full-screen intent.
 *
 * Audio goes out over [AudioAttributes.USAGE_ALARM], which is what makes this an
 * alarm rather than a loud notification: that stream ignores ringer mute and
 * silent mode, and Do Not Disturb allows it by default. The user muting their
 * phone before bed is exactly the case this feature exists for.
 */
class AlarmService : Service() {

    private var player: MediaPlayer? = null

    /** Buddy's spoken line, alive only for the seconds it is speaking. */
    private var voicePlayer: MediaPlayer? = null

    private var vibrator: Vibrator? = null
    private var wakeLock: PowerManager.WakeLock? = null
    private val handler = Handler(Looper.getMainLooper())
    private var escalation: Runnable? = null
    private var spokenLine: Runnable? = null
    private var reminderId: String? = null
    private var currentSchedule: AlarmSchedule? = null

    /**
     * What is ACTUALLY sounding, which is not always what was asked for.
     *
     * The requested tone can fall through to the bed clip and then to the system
     * default. The ripples time themselves against this rather than against
     * `schedule.tone`, so a fallback shows as ambient water instead of pulsing
     * confidently on a beat that no longer exists.
     */
    private var resolvedTone: String = ""

    /** Current gain of the looping bed, including the 25-second ramp. */
    private var currentGain = RAMP_START_GAIN

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val schedule = intent?.getStringExtra(EXTRA_SCHEDULE)
            ?.let { runCatching { AlarmSchedule.fromJson(org.json.JSONObject(it)) }.getOrNull() }
        if (schedule == null) {
            stopSelf()
            return START_NOT_STICKY
        }
        reminderId = schedule.reminderId
        currentSchedule = schedule

        // startForeground FIRST and unconditionally. Android kills a service that
        // takes more than a few seconds to post its notification, and everything
        // below (audio focus, media decode) can block.
        createChannel()
        startForeground(NOTIFICATION_ID, buildNotification(schedule))

        acquireWakeLock()
        startRinging(schedule)
        scheduleGiveUp()

        // START_NOT_STICKY: if the OS kills this mid-ring, do NOT restart it with
        // a null intent later. A recreated alarm service with no schedule would
        // ring for nothing, possibly hours after the fact.
        return START_NOT_STICKY
    }

    override fun onDestroy() {
        escalation?.let { handler.removeCallbacks(it) }
        spokenLine?.let { handler.removeCallbacks(it) }
        // Cancels the volume ramp steps too, which are plain posted lambdas.
        handler.removeCallbacksAndMessages(null)
        runCatching { player?.stop() }
        runCatching { player?.release() }
        player = null
        runCatching { voicePlayer?.stop() }
        runCatching { voicePlayer?.release() }
        voicePlayer = null
        // Before the vibrator, so the ripple view stops emitting on a beat the
        // moment the sound it was following is gone.
        AlarmStore.clearRingAnchor(applicationContext)
        runCatching { vibrator?.cancel() }
        vibrator = null
        runCatching { if (wakeLock?.isHeld == true) wakeLock?.release() }
        wakeLock = null
        super.onDestroy()
    }

    // Ringing --------------------------------------------------------------

    /**
     * Start the noise, in the order that matters if any step fails.
     *
     * A known bundled slug opens its APK asset; `device` opens only the stored
     * device URI; anything missing, unknown, or unreadable lands on the system
     * default alarm sound. A tone is a preference; ringing is the feature.
     */
    private fun startRinging(schedule: AlarmSchedule) {
        val tone = schedule.tone
        val started = when {
            tone == AlarmTones.DEVICE -> playDeviceTone() || playSystemDefault()
            AlarmTones.bundled(tone) != null -> playTone(tone) || playSystemDefault()
            else -> playSystemDefault()
        }

        if (started) {
            // The anchor the ripples time themselves against, written the moment
            // the audio is actually running rather than when the service was
            // asked to start. See AlarmStore.markRingStarted.
            AlarmStore.markRingStarted(applicationContext, resolvedTone, System.currentTimeMillis())
            rampVolume()
            scheduleSpokenLine(schedule)
        } else {
            Log.e(TAG, "alarm audio failed entirely; vibration only")
        }

        if (schedule.vibrate) startVibration()
    }

    /** Play a bundled tone by slug. False when it is not bundled or will not open. */
    private fun playTone(slug: String): Boolean {
        val descriptor = AlarmTones.openAsset(this, slug) ?: return false
        return descriptor.use { fd ->
            try {
                player = buildPlayer().apply {
                    setDataSource(fd.fileDescriptor, fd.startOffset, fd.length)
                    isLooping = true
                    prepare()
                    setVolume(RAMP_START_GAIN, RAMP_START_GAIN)
                    start()
                }
                resolvedTone = slug
                true
            } catch (t: Throwable) {
                Log.e(TAG, "bundled tone $slug failed to play", t)
                releasePlayer()
                false
            }
        }
    }

    /** The selected device URI. False lets the caller fall through to system. */
    private fun playDeviceTone(): Boolean {
        val uri = AlarmTones.deviceUri(this) ?: return false
        return playUri(uri, AlarmTones.DEVICE)
    }

    /** Exactly the phone's default alarm sound: the final never-silent rung. */
    private fun playSystemDefault(): Boolean {
        val uri = AlarmTones.systemDefaultUri() ?: return false
        return playUri(uri, "")
    }

    /** Play one URI and publish the slug whose timing the ripple view should use. */
    private fun playUri(uri: android.net.Uri, slug: String): Boolean {
        return try {
            player = buildPlayer().apply {
                setDataSource(this@AlarmService, uri)
                isLooping = true
                prepare()
                setVolume(RAMP_START_GAIN, RAMP_START_GAIN)
                start()
            }
            resolvedTone = slug
            true
        } catch (t: Throwable) {
            Log.e(TAG, "alarm uri failed: $uri", t)
            releasePlayer()
            false
        }
    }

    private fun buildPlayer(): MediaPlayer = MediaPlayer().apply {
        setAudioAttributes(
            AudioAttributes.Builder()
                .setUsage(AudioAttributes.USAGE_ALARM)
                .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION)
                .build(),
        )
    }

    private fun releasePlayer() {
        runCatching { player?.release() }
        player = null
    }

    /**
     * Come up from a third of the way to full over half a minute.
     *
     * Not a gimmick: the alarm stream is set loud precisely so it cannot be
     * slept through, and starting there means every alarm begins with a jolt.
     * The ramp is short enough that someone who genuinely sleeps hard is at full
     * volume well inside the ten-minute window, and it never touches the
     * device's own alarm volume, only this player's gain within it.
     */
    private fun rampVolume() {
        val active = player ?: return
        currentGain = RAMP_START_GAIN
        runCatching { active.setVolume(currentGain, currentGain) }
        val steps = (RAMP_DURATION_MS / RAMP_STEP_MS).toInt()
        for (step in 1..steps) {
            handler.postDelayed({
                val gain = RAMP_START_GAIN + (1f - RAMP_START_GAIN) * (step.toFloat() / steps)
                currentGain = gain
                runCatching { player?.setVolume(currentGain, currentGain) }
            }, step * RAMP_STEP_MS)
        }
    }

    /**
     * Buddy reads the reminder aloud, once, part way into the ring.
     *
     * Only for the `buddy` tone, and only when Dart managed to cache a clip
     * before now. The tone is paused for the length of the line and resumed
     * after, so a failure here is inaudible: the tone simply never stops.
     *
     * Deliberately not looped and not repeated. Hearing the same sentence every
     * twenty seconds for ten minutes would be a worse alarm than the tone alone.
     */
    private fun scheduleSpokenLine(schedule: AlarmSchedule) {
        if (schedule.tone != AlarmTones.BUDDY) return
        val path = schedule.voiceClipPath
        if (path.isBlank() || !java.io.File(path).let { it.exists() && it.length() > 0 }) {
            Log.i(TAG, "buddy tone with no cached clip; the tone rings alone")
            return
        }

        val task = Runnable {
            try {
                val bed = player
                runCatching { bed?.pause() }
                voicePlayer = buildPlayer().apply {
                    setDataSource(path)
                    isLooping = false
                    setOnCompletionListener { finished ->
                        runCatching { finished.release() }
                        voicePlayer = null
                        // The ramp keeps advancing while the bed is paused. Resume
                        // at its real gain rather than jumping to full volume.
                        runCatching { bed?.setVolume(currentGain, currentGain) }
                        runCatching { bed?.start() }
                    }
                    setOnErrorListener { failed, what, extra ->
                        Log.e(TAG, "spoken wake line failed ($what/$extra)")
                        runCatching { failed.release() }
                        voicePlayer = null
                        runCatching { bed?.start() }
                        true
                    }
                    prepare()
                    start()
                }
            } catch (t: Throwable) {
                Log.e(TAG, "spoken wake line could not start", t)
                voicePlayer = null
                runCatching { player?.start() }
            }
        }
        spokenLine = task
        handler.postDelayed(task, SPEAK_AFTER_MS)
    }

    private fun startVibration() {
        vibrator = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            (getSystemService(Context.VIBRATOR_MANAGER_SERVICE) as? VibratorManager)?.defaultVibrator
        } else {
            @Suppress("DEPRECATION")
            getSystemService(Context.VIBRATOR_SERVICE) as? Vibrator
        }
        val device = vibrator ?: return
        if (!device.hasVibrator()) return

        try {
            // Escalating: short taps first, then progressively longer buzzes, then
            // a sustained one. A flat pattern is easy to sleep through; a pattern
            // that keeps getting more insistent is not.
            val effect = VibrationEffect.createWaveform(ESCALATING_PATTERN, REPEAT_FROM_INDEX)
            // USAGE_ALARM on the vibration too, not just the audio. It is what
            // exempts the buzz from Do Not Disturb and from the system's
            // "vibrate off" setting, the same way the audio stream is exempt.
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                device.vibrate(
                    effect,
                    VibrationAttributes.createForUsage(VibrationAttributes.USAGE_ALARM),
                )
            } else {
                @Suppress("DEPRECATION")
                device.vibrate(
                    effect,
                    AudioAttributes.Builder()
                        .setUsage(AudioAttributes.USAGE_ALARM)
                        .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION)
                        .build(),
                )
            }
        } catch (t: Throwable) {
            Log.e(TAG, "alarm vibration failed", t)
        }
    }

    private fun acquireWakeLock() {
        val power = getSystemService(Context.POWER_SERVICE) as? PowerManager ?: return
        wakeLock = power.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, WAKE_LOCK_TAG).apply {
            setReferenceCounted(false)
            // Timeout matches the give-up window. A wake lock leaked past the end
            // of an alarm would drain the battery all day and the user would never
            // connect it to the 3 AM alarm that caused it.
            acquire(GIVE_UP_AFTER_MS)
        }
    }

    /**
     * Stop after ten minutes of being ignored.
     *
     * A real alarm clock gives up too. Ringing indefinitely on a phone left at
     * home is worse for the user than stopping, and it is the difference between
     * a flat battery and a missed alarm.
     */
    private fun scheduleGiveUp() {
        val task = Runnable {
            Log.i(TAG, "alarm gave up unanswered: $reminderId")
            reminderId?.takeIf { currentSchedule?.isLocalRegular != true }?.let {
                AlarmStore.queueAck(applicationContext, it, ACK_UNANSWERED, null)
            }
            stopSelf()
        }
        escalation = task
        handler.postDelayed(task, GIVE_UP_AFTER_MS)
    }

    // Notification ---------------------------------------------------------

    private fun createChannel() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
        val manager = getSystemService(Context.NOTIFICATION_SERVICE) as? NotificationManager ?: return
        // IMPORTANCE_HIGH plus USAGE_ALARM audio attributes is what makes the OS
        // treat this as an alarm channel: it becomes eligible for Do Not Disturb's
        // "Alarms" allowance, which the default aura_default channel is not.
        //
        // A channel's importance and sound are IMMUTABLE after creation, so this
        // must be right the first time; a later change needs a new channel id.
        manager.createNotificationChannel(alarmChannel(CHANNEL_ID, "Alarms", vibrate = true))
        manager.createNotificationChannel(
            alarmChannel(
                CHANNEL_ID_NO_VIBRATION,
                "Alarms (sound only)",
                vibrate = false,
            ),
        )
    }

    private fun alarmChannel(id: String, name: String, vibrate: Boolean): NotificationChannel =
        NotificationChannel(id, name, NotificationManager.IMPORTANCE_HIGH).apply {
            description = "Alarms Buddy sets when you ask to be woken up."
            setSound(
                RingtoneManager.getDefaultUri(RingtoneManager.TYPE_ALARM),
                AudioAttributes.Builder()
                    .setUsage(AudioAttributes.USAGE_ALARM)
                    .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION)
                    .build(),
            )
            enableVibration(vibrate)
            if (vibrate) vibrationPattern = ESCALATING_PATTERN
            setBypassDnd(true)
            lockscreenVisibility = Notification.VISIBILITY_PUBLIC
            setShowBadge(false)
        }

    private fun buildNotification(schedule: AlarmSchedule): Notification {
        val full = PendingIntent.getActivity(
            this,
            AlarmScheduler.requestCode(schedule.reminderId),
            AlarmActivity.launchIntent(this, schedule),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        val channelId = if (schedule.vibrate) CHANNEL_ID else CHANNEL_ID_NO_VIBRATION
        val builder = Notification.Builder(this, channelId)
            .setSmallIcon(R.drawable.ic_notification)
            .setContentTitle("Buddy")
            .setContentText(schedule.message.ifBlank { "Time to get up." })
            .setCategory(Notification.CATEGORY_ALARM)
            .setOngoing(true)
            .setAutoCancel(false)
            .setContentIntent(full)
            // The full-screen intent is the upgrade, not the mechanism. On
            // Android 14+ it may be denied, in which case this degrades to a
            // heads-up notification, and the service is still holding a wake
            // lock and playing at alarm volume, so the user is still woken.
            .setFullScreenIntent(full, true)
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) {
            @Suppress("DEPRECATION")
            builder.setPriority(Notification.PRIORITY_MAX)
            if (!schedule.vibrate) {
                @Suppress("DEPRECATION")
                builder.setVibrate(longArrayOf(0L))
            }
        }
        return builder.build()
    }

    companion object {
        private const val TAG = "AuraAlarm"
        private const val EXTRA_SCHEDULE = "schedule_json"
        private const val WAKE_LOCK_TAG = "aura:alarm"

        const val CHANNEL_ID = "aura_alarm"
        private const val CHANNEL_ID_NO_VIBRATION = "aura_alarm_sound_only"
        const val NOTIFICATION_ID = 90210
        const val ACK_UNANSWERED = "unanswered"

        const val GIVE_UP_AFTER_MS = 10L * 60L * 1000L

        /**
         * Volume ramp. Starts audible rather than near-silent: this is an alarm,
         * and a fade from zero is how someone sleeps through the first ten
         * seconds of one.
         */
        private const val RAMP_START_GAIN = 0.35f
        private const val RAMP_DURATION_MS = 25_000L
        private const val RAMP_STEP_MS = 500L

        /**
         * How long the tone rings before Buddy speaks. Long enough that the
         * sound has done its job of surfacing the sleeper, so the sentence lands
         * on someone who can hear it rather than into a dream.
         */
        private const val SPEAK_AFTER_MS = 20_000L

        /** off, buzz, off, buzz... each pair longer than the last. */
        private val ESCALATING_PATTERN = longArrayOf(
            0, 400, 600,
            0, 600, 500,
            0, 900, 400,
            0, 1400, 300,
        )

        /** Loop back to the longest buzzes rather than restarting soft. */
        private const val REPEAT_FROM_INDEX = 6

        fun startIntent(context: Context, schedule: AlarmSchedule): Intent =
            Intent(context, AlarmService::class.java)
                .putExtra(EXTRA_SCHEDULE, schedule.toJson().toString())

        fun stop(context: Context) {
            runCatching { context.stopService(Intent(context, AlarmService::class.java)) }
        }
    }
}
