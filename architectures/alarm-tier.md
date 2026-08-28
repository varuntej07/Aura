# Alarm tier

Buddy can wake someone up.

## Why this exists

A user recruited Buddy as an accountability partner for an overnight goal and
asked, at 16:09, "are you able to set an alarm at 3am for me?" Buddy answered
that it could only send a reminder and it "won't make a sound the way an alarm
would." The next morning: "hey I overslept." The one night the experiment had to
work, it died on a silent push.

## The load-bearing fact

**FCM cannot wake a doze'd phone.** No priority flag changes this; the OS decides
when a backgrounded app may make noise, and overnight the answer is "later." An
alarm has to be a schedule the OS itself holds, registered ahead of time through
`AlarmManager.setAlarmClock`.

So the backend stopped being the thing that rings and became the thing that
distributes schedules. Everything else here follows from that one inversion.

```text
set_reminder(tier="alarm")
        |
        +-- reminders/{id}  {tier, trigger_at, local_time, timezone}
        |
        +-- alarm_sync.push_schedule  -> silent data-only FCM  (fast path)
        |
        +-- GET /reminders/alarms     -> full reconcile         (authoritative)
                    |
                    v
        AlarmManager.setAlarmClock   (survives app kill, doze, and no network)
                    |
        AlarmReceiver -> AlarmService -> notification -> (user tap) -> AlarmActivity
                    |
        Dismiss / Snooze 9m / I'm up  -> queued ack -> POST /reminders/{id}/ack
                                                    -> "I'm up" opens a chat turn
```

The scheduler tick still submits a `SOURCE_REMINDER` proposal at the fire time.
For an alarm that is a **backstop**, not the delivery mechanism: cover for a
device that never held a local schedule.

The regular wake-up alarm in Settings is a separate, device-local authority. Its
weekly definition is stored once in native `SharedPreferences`; Android arms
only the next selected wall-clock occurrence and derives the following one as
soon as it fires. It never creates Firestore reminder rows and never waits for
the backend scheduler. A pending snooze uses a separate local id so tomorrow's
regular occurrence is already safe while the snooze is ringing.

## Division of labour

**Kotlin owns the schedule. Dart owns the network.**

When an alarm fires there may be no Flutter engine, no Firebase, and no network,
so nothing on the Dart side can be on the ringing path. Dart holds the live auth
token, so nothing on the Kotlin side can talk to the backend. Every design
decision below is a consequence:

- The dedupe ledger and the armed set live in native `SharedPreferences`
  (`AlarmStore`), written with `commit()` rather than `apply()` because the
  calling process can die the instant `onReceive` returns.
- Dismiss and Snooze **queue** their ack instead of sending it. The device has
  already done the part that matters (it stopped, it re-armed); the server
  catches up on the next app open.
- A queued snooze carries `next_trigger_at`, the exact moment the device armed.
  Without it, an ack flushed hours later would record a snooze counted from the
  flush. A `next_trigger_at` already in the past settles the row as dismissed
  rather than resurrecting an alarm the user is done with.

## Sound and wake-line contract

The backend resolves `reminders/{id}.tone` over
`users/{uid}.settings.alarm_tone` and sends one concrete slug. Android opens a
known bundled Flutter asset, opens the stored system-picker URI for `device`, or
falls through to the phone's default alarm sound for every missing, unknown, or
failed source. The looping player ramps from 0.35 to 1.0 over 25 seconds; the
existing alarm vibration remains independent so audio failure cannot suppress it.

`buddy` uses Morning Clock as its bed. `GET /reminders/{id}/wake-clip` renders the
verbatim `It's {local time}. {message}` line with the user's entitlement-resolved
Buddy voice. Dart fetches it while arming, caches it durably under the stable
message-and-voice tag, and passes only a local absolute path to Kotlin. At 20
seconds Kotlin pauses the bed, plays the MP3 once on `USAGE_ALARM`, and resumes;
any fetch, file, decode, or playback failure leaves the bed looping.

The ring-start timestamp and actual resolved slug are committed to `AlarmStore`
immediately after playback begins. `AlarmRippleView` derives emissions from that
anchor and uses a 3.2-second ambient period for tones without declared beat
metadata, device-picked audio, and default audio. It stops drawing whenever the
full-screen Activity is paused.

## Rules that are load-bearing

- **Never confirm a wake-up the device cannot deliver.** Android 14 denies
  `SCHEDULE_EXACT_ALARM` by default, and denies it silently. See "The permission"
  below for how this is enforced end to end. Confirming anyway is the original
  bug wearing a new coat.
- **`SCHEDULE_EXACT_ALARM`, never `USE_EXACT_ALARM`.** The latter is granted
  automatically but Play policy restricts it to apps whose core function is an
  alarm clock or calendar. Aura is a companion app; declaring it invites a
  takedown of the whole listing.
- **No `USE_FULL_SCREEN_INTENT`, ever.** It was declared, and on 2026-08-28 Play
  rejected the submission under the Full-Screen Intent Permission policy: the
  permission is reserved for apps whose core purpose is calling or alarms, and a
  companion app does not qualify. The same reasoning as `USE_EXACT_ALARM`, learnt
  the expensive way. Removing it costs only the automatic lock-screen takeover:
  the alarm still rings at alarm volume, vibrates, bypasses DND, and holds the
  wake lock, and the user taps the notification to reach `AlarmActivity`, which
  still displays over the lock screen through its own `showWhenLocked` /
  `turnScreenOn` attributes. Do not restore it, and do not simulate it by
  launching the Activity directly from `AlarmService`: that reads as evading the
  policy finding and risks the listing.
- **Snooze keeps `status = "pending"`.** `fetch_due_reminders` selects on
  `status == "pending"` and nothing else, so a `snoozed` status would drop the row
  out of the backstop scan permanently and the 3 AM snooze would quietly end the
  accountability. Snooze moves `trigger_at` and increments `snooze_count`; the
  `snoozed` value the Flutter model declares has never had a producer and must not
  get one.
- **Alarms are wall-clock-anchored; reminders are instant-anchored.** "Wake me at
  6" means 6 AM where the sleeper is, so `local_time` + `timezone` are stored and
  the device re-resolves them against its own zone. A shift beyond three hours is
  a timezone move rather than DST, and is deliberately NOT applied silently: the
  original instant is kept for the app to confirm with the user.
- **The alarm backstop is sent data-only.** Android draws its own banner from a
  notification block with no chance to ask whether the local alarm already rang,
  so the client must own that decision. `AlarmService.handleFallback` renders it
  or swallows it based on the native fired ledger.
- **`GET /reminders/alarms` returns 503, never an empty 200, on failure.** The
  answer is complete, so an empty list is an instruction to disarm everything.
  Returning one on a Firestore blip would cancel every alarm the user has.
- **The reconcile replaces the server-owned partition, not device-local
  alarms.** A server alarm absent from the answer was cancelled elsewhere and
  must stop ringing here. The Settings-owned regular alarm and its snooze have
  local source markers and survive that replacement.

## The permission

`setAlarmClock()` throws a `SecurityException` without `SCHEDULE_EXACT_ALARM`,
and from Android 14 that permission is not pre-granted to apps targeting API 33+.
There is no runtime dialog for it, only a Settings screen. Four pieces close the
loop, and none of them work alone:

1. **The device reports.** `POST /devices/register` carries `alarm_capable`,
   stored per-token because one user can hold the permission on their phone and
   not their tablet. `any_alarm_capable_device` ORs across them.
2. **Buddy's promise is conditional.** `_set_reminder` reads that flag and varies
   its `instruction`. **`None` must never read as "cannot ring"**: it means no
   device has reported either way, which is every user on an older app build, and
   telling someone their alarm will not work when it will is its own failure. Only
   an explicit `False` downgrades the promise. The handler rejects a non-bool
   rather than coercing, because a truthy string would claim a capability the
   device lacks.
3. **The ask lands at the right moment.** `AlarmPermissionPrompt` posts a
   notification from the arm-denied path, deep-linked to Aura's own "Alarms &
   reminders" toggle. A notification and not a dialog: alarms are set hours ahead,
   usually by voice, so the app is almost never in the foreground when the
   schedule arrives. Rate-limited to once per 24h, on its own `aura_alarm_setup`
   channel so a setup prompt can never inherit alarm volume or DND bypass.
4. **The grant is noticed.** Nothing tells the app when the user flips the switch,
   so `_syncAlarms` compares capability against the last reported value on every
   resume. A `false → true` flip re-arms everything (upgrading inexact alarms to
   real ones) and re-registers the token. This is what makes walking back from
   Settings work.

**Refusal degrades, it does not silence.** `setAndAllowWhileIdle` is not one of
the APIs that throws without the permission, so a refused alarm is still armed,
just inexactly: Doze batches these and can delay them several minutes. Those
alarms are flagged in `AlarmStore`, surfaced as `degraded_alarm_count`, and shown
in the reminders screen as "may fire a few minutes late" rather than dressed up
as alarms.

## Known holes, stated rather than papered over

- **User force-stop is unrecoverable** until the app is opened again. Android
  cancels the app's alarms and blocks its receivers, so `AlarmBootReceiver` never
  runs. The backstop push is the only cover and it is weak.
- **iOS has no alarm tier.** There is no equivalent to `setAlarmClock` without
  Apple's critical-alerts entitlement. `AlarmService.isSupported` is false there
  and every call no-ops; iOS still receives the ordinary reminder banner.
- **Desktop does not ring yet.** `desktop_outbox` has no `alarm_due` type, so an
  alarm degrades to a generic desktop notification.

## Verification

The only proof that counts is a physical device: DND on, app swiped out of
recents, screen off. Then a reboot with an alarm pending, then the same in
airplane mode. Compiling is not evidence that anything makes noise.

Health metric: the ratio of `alarm_fired` with `source: local` to `source:
fallback`. A rising fallback share means local scheduling is being killed
somewhere.

## Code anchors

- `backend/src/services/alarm_sync.py` (silent control pushes; why they bypass the funnel)
- `backend/src/handlers/reminders.py` (`GET /reminders/alarms`, alarm acks, coming-soon interest)
- `backend/src/services/tool_executor.py` (`_set_reminder` tier + `requeue_stuck_reminders`)
- `backend/src/handlers/scheduler.py` (backstop proposal, stuck-reminder sweep)
- `android/app/src/main/kotlin/dev/varuntej/aura/alarm/` (the whole ringing path)
- `lib/data/services/alarm_service.dart` (regular alarm bridge, reconcile, ack and interest queues)
