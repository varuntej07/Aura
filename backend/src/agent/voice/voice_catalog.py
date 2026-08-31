"""The curated set of voices Buddy can speak in, and how a stored pick resolves.

Firestore stores a **slug** (`users/{uid}.settings.tts_voice_id`), never a raw
Cartesia UUID. The slug is the stable contract with the app; the UUID behind it
can be swapped if Cartesia retires a library voice, without stranding anyone.

Curated for companion warmth rather than the customer-support register most of
the Cartesia library is written for. `katie` is the incumbent: it is the default
the LiveKit plugin has silently used since voice shipped, so it stays the default
here and a user who has never picked constructs the exact same voice as before.

Every entry also names a Deepgram Aura-2 model. That leg is only reached in a
genuine Cartesia outage (see build_tts_pipeline), so the mapping is a coarse
gender match, not a timbre match: the goal is that Buddy keeps talking and does
not switch apparent gender mid-sentence.

Every Aura-2 model here is English (`-en`) and stays that way ON PURPOSE. Buddy now
speaks whatever language the user speaks: Cartesia sonic-3.5 covers 42 of them and
voice/spoken_language.py retunes the live legs per utterance. Aura-2 has no
counterpart in those languages, so on a Cartesia outage a Telugu speaker hears an
English-sounding Buddy. That is the accepted trade, not an oversight. The
alternative is dropping the leg for non-English sessions, which converts a rare
degraded call into a rare dead one.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BuddyVoice:
    """One selectable voice. `slug` is what Firestore stores."""

    slug: str
    cartesia_voice_id: str
    deepgram_model: str
    paid_only: bool


# Display labels and descriptions deliberately live client-side next to the
# bundled preview clips. The backend owns resolution and entitlement only.
CATALOG: tuple[BuddyVoice, ...] = (
    # Free. `katie` is today's voice for every existing user; changing it would
    # silently re-voice the entire installed base on their next call.
    BuddyVoice("katie", "f786b574-daa5-4673-aa0c-cbe3e8534c02", "aura-2-andromeda-en", False),
    BuddyVoice("dallas", "23e9e50a-4ea2-447b-b589-df90dbb848a2", "aura-2-apollo-en", False),
    # Paid.
    BuddyVoice("tessa", "6ccbfb76-1fc6-48f7-b71d-91ac6298247b", "aura-2-andromeda-en", True),
    BuddyVoice("kira", "57dcab65-68ac-45a6-8480-6c4c52ec1cd1", "aura-2-andromeda-en", True),
    BuddyVoice("layla", "999df508-4de5-40a7-8bd3-8c12f678c284", "aura-2-andromeda-en", True),
    BuddyVoice("jolene", "d1d9c946-7cfc-4378-85a4-07d09827cb7e", "aura-2-andromeda-en", True),
    BuddyVoice("kyle", "c961b81c-a935-4c17-bfb3-ba2239de8c2f", "aura-2-apollo-en", True),
    BuddyVoice("archie", "ef191366-f52f-447a-a398-ed8c0f2943a1", "aura-2-apollo-en", True),
)

DEFAULT_VOICE_SLUG = "katie"

_BY_SLUG: dict[str, BuddyVoice] = {voice.slug: voice for voice in CATALOG}
DEFAULT_VOICE: BuddyVoice = _BY_SLUG[DEFAULT_VOICE_SLUG]

# Why a resolve() fell back, for the caller to log. "" means the pick was honored.
REASON_UNSET = "unset"
REASON_UNKNOWN = "unknown_slug"
REASON_TIER_LOCKED = "tier_locked"


def resolve_voice(voice_id: str, user_tier: str) -> tuple[BuddyVoice, str]:
    """Resolve a stored slug to a voice, returning (voice, fallback_reason).

    The stored value is untrusted: firestore.rules lets the client write anything
    under users/{uid} and does no enum validation, so an unknown slug must land on
    the default rather than reaching Cartesia as a bogus voice id.

    Tier is checked here because a paid voice must not survive a lapse. It fails
    *open* by testing for "free" explicitly rather than membership in PAID_TIERS:
    gather_session_context declares "unknown" as user_tier's fallback when the
    entitlement read fails or times out, and an allow-list would silently lock
    every paid voice during a Firestore hiccup. Losing the voice you picked
    because of an outage is worse than a lapsed user keeping one for a session.

    Returns the default with a non-empty reason on every fallback path so the
    caller can log it. A silent fallback here would be indistinguishable from a
    healthy resolve.
    """
    slug = (voice_id or "").strip().lower()
    if not slug:
        return DEFAULT_VOICE, REASON_UNSET

    voice = _BY_SLUG.get(slug)
    if voice is None:
        return DEFAULT_VOICE, REASON_UNKNOWN

    if voice.paid_only and (user_tier or "").strip().lower() == "free":
        return DEFAULT_VOICE, REASON_TIER_LOCKED

    return voice, ""
