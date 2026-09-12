"""The shape of the home-screen deck, and the validation that keeps it honest.

Every card is one thing the user can do in a single tap, and the catalog of kinds
is closed by a Literal rather than by inspecting anything the user said: the model
chooses from the catalog, and the schema makes any other choice unrepresentable.

The validation below exists for one reason. A card face is a promise ("a joke
every morning"), and the confirm button has to be able to keep it. So a card whose
action cannot actually be performed — a ritual with no valid schedule, a navigate
card pointing somewhere that does not exist — is dropped here rather than shipped
to the phone to fail under someone's finger.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# Where a navigate card is allowed to send someone. An allowlist rather than a
# free string: the deck is model-generated, and a route it invents is a dead end.
NAVIGABLE_ROUTES = ("/briefing", "/settings/connectors")

KIND_TO_ACTION = {
    "ritual": "ritual_create",
    "remind": "reminder_create",
    "track": "tracker_create",
    "talk": "chat_open",
    "briefing": "navigate",
    "connect": "navigate",
}


class RitualSchedule(BaseModel):
    freq: Literal["daily", "weekly"]
    hour: int
    minute: int
    weekdays: list[int] = Field(default_factory=list)


class ReminderLocalTime(BaseModel):
    hour: int
    minute: int
    day_offset: int = 0


class HomeDeckOption(BaseModel):
    id: str
    label: str
    schedule: RitualSchedule | None = None
    local_time: ReminderLocalTime | None = None


class HomeDeckSuggestion(BaseModel):
    """One phrase the card face flashes, and the thing confirming it creates.

    A suggestion deliberately carries no time of its own: the card's `options`
    are the only source of truth for when something fires. Two sources would let
    the face advertise a time the confirm button does not set, which is the exact
    failure this module exists to prevent.
    """

    id: str
    text: str
    content_kind: Literal["joke", "motivation", "checkin", "question"] | None = None


class HomeDeckAction(BaseModel):
    type: Literal[
        "ritual_create",
        "reminder_create",
        "tracker_create",
        "chat_open",
        "navigate",
    ]
    content_kind: Literal["joke", "motivation", "checkin", "question"] | None = None
    message: str = ""
    request: str = ""
    opening_message: str = ""
    route: str = ""
    default_option_id: str = ""


class HomeDeckCard(BaseModel):
    kind: Literal["ritual", "remind", "track", "talk", "briefing", "connect"]
    title: str
    subtitle: str = ""
    primary_label: str = ""
    options: list[HomeDeckOption] = Field(default_factory=list)
    suggestions: list[HomeDeckSuggestion] = Field(default_factory=list)
    action: HomeDeckAction


class HomeDeckBundle(BaseModel):
    """One generation covering both home surfaces.

    The chat starters ride along rather than taking a second model call: they need
    exactly the same grounding, so splitting them would double the call count for
    this subsystem at every user count.
    """

    cards: list[HomeDeckCard] = Field(default_factory=list)
    pills: list[str] = Field(default_factory=list)


def _valid_schedule(schedule: RitualSchedule | None) -> bool:
    if schedule is None:
        return False
    if not 0 <= schedule.hour <= 23 or not 0 <= schedule.minute <= 59:
        return False
    days = {int(day) for day in schedule.weekdays}
    if schedule.freq == "weekly":
        return bool(days) and all(0 <= day <= 6 for day in days)
    return not days


def _valid_local_time(local_time: ReminderLocalTime | None) -> bool:
    if local_time is None:
        return False
    return (
        0 <= local_time.hour <= 23
        and 0 <= local_time.minute <= 59
        and 0 <= local_time.day_offset <= 7
    )


def _clean(text: str, *, max_words: int, max_chars: int) -> str:
    collapsed = " ".join(str(text or "").strip().split())
    if not collapsed or len(collapsed.split()) > max_words:
        return ""
    return collapsed[:max_chars]


# How many phrases one card may flash. More than this and the rotation is longer
# than anyone watches before tapping.
MAX_SUGGESTIONS_PER_CARD = 5

# The two slots the model does not write. Their routes are already the closed
# allowlist above, so there is nothing for a model to get wrong and no reason to
# spend tokens generating them. They are still written to the deck document
# because installs that predate the fixed-four hand read four cards from it.
STATIC_BRIEFING_CARD: dict = {
    "kind": "briefing",
    "title": "Today's briefing",
    "subtitle": "What I picked up for you while you were away.",
    "primary_label": "Read it",
    "options": [],
    "suggestions": [],
    "action": {"type": "navigate", "route": "/briefing"},
}

STATIC_CONNECT_CARD: dict = {
    "kind": "connect",
    "title": "Connect your world",
    "subtitle": "Calendar and mail, so I know what your week looks like.",
    "primary_label": "Connect",
    "options": [],
    "suggestions": [],
    "action": {"type": "navigate", "route": "/settings/connectors"},
}


def _sanitize_suggestions(card: HomeDeckCard, title: str) -> list[dict]:
    """The phrases a card flashes, each one a thing confirm can actually create.

    Never returns empty. A card whose suggestions all failed validation falls back
    to its own already-validated title, which is exactly what an older client
    renders anyway, so a model that ignored the field costs the user a card that
    does not flash rather than a card that is not there.
    """
    suggestions: list[dict] = []
    seen: set[str] = set()
    for suggestion in card.suggestions:
        text = _clean(suggestion.text, max_words=10, max_chars=70)
        if not suggestion.id or not text or text.casefold() in seen:
            continue
        seen.add(text.casefold())
        entry: dict = {"id": suggestion.id, "text": text}
        if card.kind == "ritual":
            # Coerced the same way rituals.normalize_content_kind would coerce it
            # server-side, so the card cannot promise a kind the service silently
            # changes underneath it.
            entry["content_kind"] = suggestion.content_kind or "checkin"
        suggestions.append(entry)
        if len(suggestions) == MAX_SUGGESTIONS_PER_CARD:
            break

    if not suggestions:
        fallback: dict = {"id": "s0", "text": title}
        if card.kind == "ritual":
            fallback["content_kind"] = card.action.content_kind or "checkin"
        return [fallback]
    return suggestions


def sanitize_cards(cards: list[HomeDeckCard]) -> list[dict]:
    """Drop every card the app could not honour, and serialize the rest.

    Order is preserved: the model's first choices are the ones the user sees, and
    the client shows four of them with the remainder held as replacements.
    """
    serialized: list[dict] = []
    for card in cards:
        if KIND_TO_ACTION.get(card.kind) != card.action.type:
            continue
        title = _clean(card.title, max_words=8, max_chars=60)
        if not title:
            continue

        options: list[dict] = []
        if card.kind == "ritual":
            for option in card.options:
                schedule = option.schedule
                if not option.id or not _valid_schedule(schedule) or schedule is None:
                    continue
                options.append({
                    "id": option.id,
                    "label": _clean(option.label, max_words=5, max_chars=32),
                    "schedule": schedule.model_dump(),
                })
            if not options:
                continue
        elif card.kind == "remind":
            for option in card.options:
                local_time = option.local_time
                if not option.id or not _valid_local_time(local_time) or local_time is None:
                    continue
                options.append({
                    "id": option.id,
                    "label": _clean(option.label, max_words=5, max_chars=32),
                    "local_time": local_time.model_dump(),
                })
            if not options:
                continue
        elif card.kind == "track" and not card.action.request.strip():
            continue
        elif card.kind == "talk" and not card.action.opening_message.strip():
            continue

        # The phrases the face flashes. The first one is also what `title` and, for
        # a reminder, `action.message` are set to below: those two fields are what
        # an install predating this field renders and posts, so they have to carry
        # the primary phrase rather than a card label.
        suggestions = _sanitize_suggestions(card, title)
        if card.kind in ("ritual", "remind"):
            title = suggestions[0]["text"]

        action: dict = {"type": card.action.type}
        if card.action.type == "ritual_create":
            action["content_kind"] = (
                suggestions[0].get("content_kind")
                or card.action.content_kind
                or "checkin"
            )
        elif card.action.type == "reminder_create":
            action["message"] = suggestions[0]["text"][:500]
        elif card.action.type == "tracker_create":
            action["request"] = card.action.request.strip()[:300]
        elif card.action.type == "chat_open":
            action["opening_message"] = card.action.opening_message.strip()[:400]
        else:
            route = card.action.route.strip() or (
                "/briefing" if card.kind == "briefing" else "/settings/connectors"
            )
            if route not in NAVIGABLE_ROUTES:
                continue
            action["route"] = route
        if options:
            default_id = card.action.default_option_id
            action["default_option_id"] = (
                default_id
                if any(option["id"] == default_id for option in options)
                else options[0]["id"]
            )

        serialized.append({
            "kind": card.kind,
            "title": title,
            "subtitle": _clean(card.subtitle, max_words=14, max_chars=90),
            "primary_label": _clean(card.primary_label, max_words=4, max_chars=24)
            or "Let's do it",
            "options": options,
            "suggestions": suggestions,
            "action": action,
        })
    return serialized


def clean_pills(pills: list[str]) -> list[str]:
    """Same contract the shipped clients already expect: short first-person starters."""
    valid = [
        " ".join(pill.strip().split())
        for pill in pills
        if isinstance(pill, str) and pill.strip() and len(pill.strip().split()) <= 6
    ]
    return valid[:5]
