"""The sounds an alarm may ring with, and how a stored pick resolves.

Mirrors `lib/core/constants/alarm_tones.dart` and
`android/app/src/main/kotlin/dev/varuntej/aura/alarm/AlarmTones.kt`. The Kotlin
copy is the one that makes noise; this one exists so a slug that reaches the
device is always one the device can play.

Two stores, two meanings:

- ``users/{uid}.settings.alarm_tone`` is the user's default, set in the app.
- ``reminders/{id}.tone`` is a per-alarm override, which Buddy sets when someone
  asks for a specific one ("wake me at 6 with something gentle").

:func:`resolve_tone` collapses those to the single concrete slug that ships in
the sync row, so the device never has to know the precedence rule.
"""

from __future__ import annotations

# Bundled clips. Order mirrors the picker.
BUNDLED_TONES: tuple[str, ...] = (
    "morning-clock-alarm",
    "alert-alarm",
    "buzzer-alarm",
    "warning-buzzer",
    "street-public-alarm",
    "battleship-alarm",
    "retro-game-emergency",
    "rooster-crowing",
    "short-rooster-crowing",
)

# Buddy reads the reminder aloud in the user's chosen voice, over the bed clip.
# One of the tones rather than a flag beside them, so there is nothing to be off.
TONE_BUDDY = "buddy"

# A sound from the user's own device. The URI itself never leaves the phone; it
# is a path into their storage and means nothing here.
TONE_DEVICE = "device"

# Whatever the phone's default alarm sound is. Every account is on this today,
# and it is what an unresolvable pick degrades to.
TONE_SYSTEM_DEFAULT = ""

SELECTABLE_TONES: frozenset[str] = frozenset(
    BUNDLED_TONES + (TONE_BUDDY, TONE_DEVICE, TONE_SYSTEM_DEFAULT)
)

# What an LLM may choose. Neither special slug belongs here: "device" names a
# file only the phone can see, and the empty default is what omitting the
# argument already means.
ASSIGNABLE_TONES: tuple[str, ...] = BUNDLED_TONES + (TONE_BUDDY,)


def normalize_tone(value: object) -> str:
    """Coerce an untrusted stored value to a known slug, or to the default.

    firestore.rules lets the client write anything under ``users/{uid}``, and the
    tool argument is model-generated, so neither source can be trusted to hold a
    slug this build knows. An unrecognised value becomes the system default,
    which is the behaviour every alarm had before tones existed.
    """
    slug = str(value or "").strip().lower()
    return slug if slug in SELECTABLE_TONES else TONE_SYSTEM_DEFAULT


def resolve_tone(reminder_tone: object, user_default: object) -> str:
    """The one slug to send: per-alarm override, else user default, else system.

    Resolved here rather than on the device so the precedence rule lives in one
    place. The device receives a concrete answer and plays it.
    """
    override = normalize_tone(reminder_tone)
    if override:
        return override
    return normalize_tone(user_default)


def speaks_aloud(tone: str) -> bool:
    """Whether this tone needs a rendered wake-up clip cached before it rings."""
    return normalize_tone(tone) == TONE_BUDDY


async def user_default_tone(user_id: str) -> str:
    """The user's chosen default, or the system default on any failure.

    Never raises. This is read on the alarm sync path, and an alarm that rings
    with the phone's stock sound because a settings read blipped is a far better
    outcome than an alarm that does not ring at all.
    """
    import asyncio

    from .firebase import admin_firestore

    def _read() -> str:
        snapshot = admin_firestore().collection("users").document(user_id).get()
        if not snapshot.exists:
            return TONE_SYSTEM_DEFAULT
        settings = (snapshot.to_dict() or {}).get("settings") or {}
        return normalize_tone(settings.get("alarm_tone"))

    try:
        return await asyncio.to_thread(_read)
    except Exception as exc:
        from ..lib.logger import logger

        logger.warn("alarm_tones: default tone read failed; using system default", {
            "user_id": user_id,
            "error": str(exc),
            "error_type": type(exc).__name__,
        })
        return TONE_SYSTEM_DEFAULT
