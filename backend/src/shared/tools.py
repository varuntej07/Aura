"""Canonical tool contracts. ONE description per tool, for every surface.

Each entry's ``description`` is the single place selection guidance lives: what the
tool does, when to use it, when NOT to, and what the server will do with each
argument. Text chat reads these directly; the voice worker's MCP layer overwrites
FastMCP's docstring-derived metadata with them (see handlers/mcp.py).

WHY the description and not the system prompt. OpenAI's GPT-4.1 prompting guide,
section "1. Agentic Workflows" -> "Tool Calls", is explicit:

    "We encourage developers to exclusively use the tools field to pass tools,
     rather than manually injecting tool descriptions into your prompt and writing
     a separate parser for tool calls, as some have reported doing in the past."

    https://developers.openai.com/cookbook/examples/gpt4-1_prompting_guide

The stated reason is that the tools field keeps the model in distribution during
tool-calling trajectories, and the guide reports a 2% SWE-bench Verified gain over
embedding schemas in the system prompt. The function-calling guide defines
``description`` as the place for "when and how to use the function":

    https://developers.openai.com/api/docs/guides/function-calling

Consequence for this file: guidance here is NOT duplicated into any prompt. A voice
system prompt that also explains a tool gives gpt-4.1-mini two sources for one
decision, and 4.1 follows instructions more literally than its predecessors, so it
acts on the conflict rather than smoothing over it.

``strict: True`` opts a tool into structural guarantees. The same function-calling
guide requires, for strict schemas, that ``additionalProperties`` is false and that
every property appears in ``required`` (optional values are expressed as a nullable
type). Strict guarantees argument SHAPE only; it never guarantees the model chose
the right tool or a semantically correct value. That is why every time-bearing
argument is natural language resolved server-side rather than an ISO string the
model would have to compute without a clock.
"""

from copy import deepcopy
from typing import Any


REMINDER_RECEIPT_CREATED = "created"
REMINDER_RECEIPT_UPDATED = "updated"
REMINDER_RECEIPT_EXISTING = "existing"


def resolve_set_reminder_tier(supplied_tier: Any) -> str:
    """Return the caller-supplied tier, defaulting to the quiet one.

    The tier used to be inferred from the wording when the model omitted it
    ("wake me", "get me up" -> alarm). That inference decided whether a device
    goes loud and full-screen, off phrases that also appear in ordinary talk,
    and it fired on the negations it did not enumerate. The model states the
    tier in the schema; if it does not, the quiet tier is the safe default,
    because a missed alarm is recoverable and a surprise 6am siren is not.
    """
    supplied = str(supplied_tier or "").strip().lower()
    return supplied if supplied in {"reminder", "alarm"} else "reminder"

# The one list of memory categories. tool_executor._store_memory reads it from here
# rather than restating it: the two copies had already drifted from the Flutter
# client's MemoryCategory enum, and a category the client cannot parse fails its
# whole memories list read.
#
# lifestyle and interests exist because a companion app has to hold things that are
# neither a habit nor a health fact. "Loves smoking weed" had no home in the
# original five, so store_memory hard-rejected it mid-call and Buddy ended up
# explaining its own category schema out loud to the user.
MEMORY_CATEGORIES: list[str] = [
    "preferences",
    "facts",
    "habits",
    "health",
    "routines",
    "lifestyle",
    "interests",
]

# Tools a free account may not EXECUTE. They are still exposed to the model on every
# tier: the executor refuses the call with an upgrade envelope instead. Removing them
# from the tools array is what taught Buddy to answer "Aura doesn't do calendar", which
# is false and loses the sale. See ToolExecutor.execute.
# Same membership as the old claude_client.STARTER_ONLY_TOOLS: this change moves where
# the gate is enforced, it does not change who is entitled to what.
TIER_GATED_TOOLS: frozenset[str] = frozenset({
    "create_calendar_event",
    "get_upcoming_events",
})

# The capabilities Buddy must never appear to lack, on any surface, on any turn. No
# per-turn wording heuristic, semantic selector, or surface allowlist may remove one of
# these while it is structurally eligible. Checked at startup (main.py) so a regression
# is loud instead of silent.
#
# Deliberately one write and four reads. Every additional write in the floor is a real
# risk of an unprompted call, and set_reminder is the one with evidence behind it.
CORE_TOOLS: frozenset[str] = frozenset({
    "get_aura_product_info",
    "set_reminder",
    "list_reminders",
    "get_upcoming_events",
    "query_memory",
})

# Canonical tool specs

GET_AURA_PRODUCT_INFO_TOOL_DEFINITION: dict[str, Any] = {
    "name": "get_aura_product_info",
    "description": (
        "Read Aura's verified product guide. Use when the user asks what Aura or Buddy "
        "can do, how to find or configure something in Aura, whether a feature is "
        "available on a device or plan, how Aura handles product data or privacy, "
        "what Aura is, or how to troubleshoot an Aura feature. This is Aura product "
        "knowledge only: never use it for questions about other apps, software, "
        "websites, or devices — even ones whose names sound like Aura features — and "
        "not for general facts, personal memories, calendar or reminder contents, or "
        "to perform an action. Understand the user's meaning in any language, dialect, "
        "spelling, or regional phrasing, then put a short English semantic retrieval "
        "query in query while preserving exact Aura feature names. Choose "
        "target_surface and target_platform from conversation context; current means "
        "the surface or platform they are using now. If they name a different device, "
        "select it explicitly so unavailable clients are never given instructions for "
        "another platform. The returned guide answer comes from keyword retrieval: "
        "judge whether it truly answers the user's question before using it, and if "
        "it does not, say the guide does not cover that and answer from your own "
        "knowledge or another tool such as web search."
    ),
    "strict": True,
    "inputSchema": {
        "type": "object",
        "properties": {
            "kind": {
                "type": "string",
                "enum": [
                    "capabilities",
                    "how_to",
                    "availability",
                    "privacy",
                    "product_background",
                    "troubleshooting",
                ],
                "description": "The product-information category that best fits the request.",
            },
            "query": {
                "type": "string",
                "minLength": 1,
                "maxLength": 500,
                "description": (
                    "A concise English semantic query for local retrieval. Translate or "
                    "normalize regional wording, but preserve named Aura features."
                ),
            },
            "target_surface": {
                "type": "string",
                "enum": ["current", "app", "keyboard", "desktop", "all"],
                "description": "Which Aura surface the answer is about.",
            },
            "target_platform": {
                "type": "string",
                "enum": ["current", "android", "ios", "windows", "macos", "all"],
                "description": (
                    "Which operating-system platform the answer is about. Use current "
                    "unless the user explicitly names another platform."
                ),
            },
        },
        "required": ["kind", "query", "target_surface", "target_platform"],
        "additionalProperties": False,
    },
}

SET_REMINDER_TOOL_DEFINITION: dict[str, Any] = {
    "name": "set_reminder",
    "description": (
        "Create one new reminder. The current user turn must itself ask to create "
        "or change a reminder, or answer, refine, or correct your immediately "
        "preceding reminder question. Never resurrect a reminder request from older "
        "conversation history after the user has moved to another task. Do NOT use "
        "to read existing "
        "reminders (use list_reminders) or to cancel one (use cancel_reminder). "
        "When the user hands you the decision ('you decide', 'whatever works'), fill "
        "every fillable detail from the conversation and act; ask only when a detail "
        "is genuinely unknowable, and then exactly one short question, never a stack. "
        "Never invent a date or time. Pass the time as natural language; the server "
        "resolves it against the user's current local date, time, and timezone, and "
        "rejects or clarifies what it cannot resolve safely. Always choose a tier, and "
        "always tell the user which one you chose in your reply."
    ),
    "strict": True,
    "inputSchema": {
        "type": "object",
        "properties": {
            "message": {"type": "string", "description": "What to remind the user about."},
            "when": {
                "type": "string",
                "description": (
                    "When the user wants the reminder, in natural language, for example "
                    "'tomorrow at 9 AM' or 'in 45 minutes'. Include an exact time."
                ),
            },
            "tier": {
                "type": "string",
                "enum": ["reminder", "alarm"],
                # Required AND defaulted, which is not a contradiction here. Required
                # so the model has to state the loudness on every call instead of
                # letting silence decide. Defaulted so that a model which omits it
                # anyway, or sends "Alarm", degrades to the quiet tier rather than
                # failing the whole call: a rejected set_reminder is a user being
                # told to go use their clock app, which is worse than a banner.
                "default": "reminder",
                "description": (
                    "How loud this needs to be. 'alarm' rings at alarm volume, turns "
                    "the screen on, and pierces Do Not Disturb: use it ONLY when the "
                    "user is asking to be woken or physically pulled out of what they "
                    "are doing ('set an alarm', 'wake me at 6', 'make sure I'm up', "
                    "'get me up', 'don't let me sleep through it'). 'reminder' is a "
                    "silent notification banner and is the default for everything "
                    "else, including timed tasks like 'remind me to take my meds at "
                    "8am'. Time of day alone NEVER makes something an alarm. When you "
                    "are unsure, choose 'reminder': a wrong 3 AM ring costs far more "
                    "than a missed banner, and the user can always ask again."
                ),
            },
            "tone": {
                "type": "string",
                # Optional and defaulted: "" is a real answer meaning "whatever
                # they already chose". Omitting sound detail must never reject an
                # otherwise valid alarm tool call.
                # Overriding someone's own alarm sound because a model felt
                # creative is a worse failure than never overriding it at all.
                "enum": ["", "ripple", "dawn", "tide", "chime", "pulse", "ascent", "buddy"],
                "default": "",
                "description": (
                    "Which sound this ONE alarm rings with, overriding the user's "
                    "own setting. Ignored when tier is 'reminder'. Use \"\" unless "
                    "the user asked for a specific character of sound in this "
                    "message. 'ripple' soft wooden droplets, 'dawn' a slow warm "
                    "bell, 'tide' the calmest, 'chime' bright glass bells, 'pulse' "
                    "two firm insistent notes, 'ascent' climbs and gets louder for "
                    "a heavy sleeper, 'buddy' means you read the reminder out loud "
                    "in your own voice. Map what they asked for: 'something gentle' "
                    "-> 'dawn', 'wake me properly' or 'I sleep through everything' "
                    "-> 'ascent', 'wake me up yourself' -> 'buddy'. When they said "
                    "nothing about the sound, \"\" is correct."
                ),
            },
        },
        "required": ["message", "when", "tier", "tone"],
        "additionalProperties": False,
    },
}

CREATE_CALENDAR_EVENT_TOOL_DEFINITION: dict[str, Any] = {
    "name": "create_calendar_event",
    "description": (
        "Create a Google Calendar event only when the user asks to create or schedule "
        "a calendar event. Do not use for calendar reads, availability questions, or "
        "reminders, and never for a change to an event that already exists "
        "(update_calendar_event owns that). Never invent a date, a time, or an "
        "attendee: if the user did not say it, ask one short question first. Pass the "
        "time as natural language; the server resolves it against their local clock."
    ),
    "strict": True,
    "inputSchema": {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "Short name for the calendar event.",
            },
            "when": {
                "type": "string",
                "description": (
                    "The user's natural-language start time and any duration or ending "
                    "time they supplied, such as 'tomorrow at 9 AM for 45 minutes' or "
                    "'Friday from 2 PM to 3:30 PM'. If the user supplied no duration or "
                    "ending time, omit one from this string; the server uses a 60-minute "
                    "duration. Never use ISO 8601."
                ),
            },
            "description": {
                "type": "string",
                "description": "Event notes, or an empty string when omitted.",
            },
            "location": {
                "type": "string",
                "description": "Event location, or an empty string when omitted.",
            },
            "attendees": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Guest email addresses to invite, or an empty array when omitted."
                ),
            },
        },
        "required": ["title", "when", "description", "location", "attendees"],
        "additionalProperties": False,
    },
}

UPDATE_CALENDAR_EVENT_TOOL_DEFINITION: dict[str, Any] = {
    "name": "update_calendar_event",
    "description": (
        "Change an event that already exists on the user's calendar. Use this "
        "whenever they adjust an event rather than ask for a new one: adding or "
        "removing a guest, moving the time, renaming it, setting a location. "
        "Follow-ups that build on an event you JUST created are updates, not new "
        "events. 'and invite sarah@example.com', 'make it an hour later', 'actually "
        "call it dinner with the team' all mean update the event you just made, "
        "never create a second one. Requires an event_id that came from a real "
        "create_calendar_event or get_upcoming_events result in this conversation; "
        "never invent one, and read the calendar first if you do not have one. "
        "Every field except event_id is optional: send an empty string or empty "
        "array for anything the user did not change, and it is left alone. "
        "Attendees are ADDED to whoever is already invited."
    ),
    "strict": True,
    "inputSchema": {
        "type": "object",
        "properties": {
            "event_id": {
                "type": "string",
                "description": (
                    "ID of the event to change, from an earlier tool result."
                ),
            },
            "title": {
                "type": "string",
                "description": "New name, or an empty string to leave it unchanged.",
            },
            "when": {
                "type": "string",
                "description": (
                    "The new natural-language start time and any duration or ending "
                    "time, such as 'Friday at 8 PM for 90 minutes'. Empty string "
                    "leaves the existing time alone. Never use ISO 8601."
                ),
            },
            "description": {
                "type": "string",
                "description": "New notes, or an empty string to leave them unchanged.",
            },
            "location": {
                "type": "string",
                "description": "New location, or an empty string to leave it unchanged.",
            },
            "attendees": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Guest emails to ADD to the event, or an empty array when the "
                    "guest list is not changing. Existing guests are kept."
                ),
            },
        },
        "required": [
            "event_id",
            "title",
            "when",
            "description",
            "location",
            "attendees",
        ],
        "additionalProperties": False,
    },
}

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    GET_AURA_PRODUCT_INFO_TOOL_DEFINITION,
    SET_REMINDER_TOOL_DEFINITION,
    {
        "name": "draft_writing",
        "description": (
            "Create approval-ready writing without sending or publishing it. Use "
            "when the user wants a LinkedIn post, tweet or X post, email, or email "
            "reply drafted. The user sees the returned draft in chat and remains "
            "responsible for approving and sending or posting it."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "skill_id": {
                    "type": "string",
                    "enum": ["linkedin_post", "tweet", "email"],
                    "description": "The writing skill that fits the requested destination.",
                },
                "brief": {
                    "type": "string",
                    "description": (
                        "The user's complete drafting request, including supplied facts, "
                        "audience, tone, constraints, and source text."
                    ),
                },
                "length": {
                    "type": "string",
                    "enum": ["short", "medium", "detailed"],
                    "description": "The requested or contextually appropriate draft length.",
                },
            },
            "required": ["skill_id", "brief", "length"],
        },
    },
    {
        "name": "list_reminders",
        "description": (
            "Read the user's existing reminders. Use when they ask what reminders "
            "they have, or to review or manage ones already set. Never turn a read "
            "request into a new reminder."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "status_filter": {
                    "type": "string",
                    "enum": ["pending", "fired", "dismissed", "all"],
                    "default": "pending",
                },
            },
        },
    },
    {
        "name": "cancel_reminder",
        "description": (
            "Cancel one pending reminder the user asked to remove. Requires a "
            "reminder id that came from a real list_reminders result in this "
            "conversation; never invent or guess an id. Use only for cancelling, "
            "never to create or reschedule (use set_reminder for those)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "reminder_id": {"type": "string", "description": "ID of the reminder to cancel."},
            },
            "required": ["reminder_id"],
        },
    },
    CREATE_CALENDAR_EVENT_TOOL_DEFINITION,
    UPDATE_CALENDAR_EVENT_TOOL_DEFINITION,
    {
        "name": "get_upcoming_events",
        "description": (
            "Read the user's calendar. Use whenever they ask about their schedule, "
            "meetings, appointments, availability, or what they have today, tomorrow, "
            "or this week. Never create an event from a read request; "
            "create_calendar_event owns that. Prefer range='today', range='tomorrow', "
            "or range='this_week'. Use start_time/end_time only when the user gives an "
            "explicit range."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "range": {
                    "type": "string",
                    "description": (
                        "Named range interpreted in the connected calendar's timezone."
                    ),
                    "enum": ["recent", "today", "tomorrow", "this_week"],
                    "default": "today",
                },
                "start_time": {
                    "type": "string",
                    "description": "Custom range start as an ISO 8601 datetime.",
                },
                "end_time": {
                    "type": "string",
                    "description": "Custom range end as an ISO 8601 datetime.",
                },
                "limit": {
                    "type": "integer",
                    "default": 10,
                    "minimum": 1,
                    "maximum": 25,
                },
                "hours_ahead": {
                    "type": "integer",
                    "description": "Legacy fallback. Prefer range instead.",
                },
            },
        },
    },
    {
        "name": "list_emails",
        "description": (
            "List recent messages from the user's connected Gmail account. Use when "
            "the user asks what email they received, wants to find an email, or wants "
            "a mailbox summary. This returns message metadata and snippets; use "
            "read_email with a returned message_id when the full message is needed."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Optional Gmail search query, or an empty string for recent mail."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 25,
                    "default": 10,
                },
            },
        },
    },
    {
        "name": "read_email",
        "description": (
            "Read one Gmail message after list_emails returned its message_id. Use "
            "only when the user asks to open, read, summarize, or answer that message. "
            "Never invent or guess a message_id."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "message_id": {
                    "type": "string",
                    "description": "Message ID returned by list_emails.",
                },
            },
            "required": ["message_id"],
        },
    },
    {
        "name": "send_email",
        "description": (
            "Send an email from the user's connected Gmail account. "
            "Always confirm the recipient, subject, and body with the user before calling this."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "Recipient email address."},
                "subject": {"type": "string"},
                "body": {"type": "string", "description": "Plain-text body of the email."},
            },
            "required": ["to", "body"],
        },
    },
    {
        "name": "store_memory",
        "description": (
            "Remember something lasting about the user: a fact, preference, habit, "
            "routine, or part of their lifestyle. Use when they tell you something "
            "true about themselves that should still matter next week. Storing an "
            "existing key overwrites it, which is how a correction is applied. Do "
            "NOT use for something to do at a time (that is set_reminder), for a "
            "calendar event, or to remove a memory (that is delete_memory). Pick the "
            "closest category; nothing the user says about themselves is unstorable."
        ),
        "strict": True,
        "inputSchema": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "Semantic key, e.g. 'bedtime'."},
                "value": {"type": "string", "description": "Value to store."},
                "category": {
                    "type": "string",
                    "enum": list(MEMORY_CATEGORIES),
                },
            },
            "required": ["key", "value", "category"],
        },
    },
    {
        "name": "query_memory",
        "description": (
            "Search what is already stored about the user, scoped to one subject "
            "they asked about ('what do you know about my diet'). Use this rather "
            "than get_user_context when the question is about one topic; "
            "get_user_context is the whole snapshot. Returning no matches means "
            "nothing is stored on that subject, never that the search broke."
        ),
        "strict": True,
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search string."},
                "category_filter": {
                    "type": "string",
                    "enum": [*MEMORY_CATEGORIES, "all"],
                    "default": "all",
                    "description": "Restrict the search to one category, or 'all'.",
                },
            },
            "required": ["query", "category_filter"],
        },
    },
    {
        "name": "delete_memory",
        "description": (
            "Forget one stored memory for good. Use when the user retracts something "
            "or asks you to erase, drop, or forget it. Overwriting it with a denial "
            "is NOT forgetting: 'I'm not actually allergic to peanuts, erase that' "
            "means delete the memory, not store that they are not allergic. Requires "
            "a memory_id from a real query_memory or get_user_context result in this "
            "conversation; never invent one. When they correct a fact rather than "
            "retract it, use store_memory with the same key instead."
        ),
        "strict": True,
        "inputSchema": {
            "type": "object",
            "properties": {
                "memory_id": {
                    "type": "string",
                    "description": "ID of the memory to delete, from an earlier tool result.",
                },
            },
            "required": ["memory_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_user_context",
        "description": "Retrieve a snapshot of the user's memories, reminders, and upcoming events.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "include_memories": {"type": "boolean", "default": True},
                "include_reminders": {"type": "boolean", "default": True},
                "include_events": {"type": "boolean", "default": True},
            },
        },
    },
    {
        "name": "web_surf",
        "description": (
            "Search the live web for current information, news, prices, scores, or any time-sensitive fact. "
            "Use when the user asks about recent events, live data, or topics that benefit from up-to-date sources. "
            "Do NOT use for things you already know or for the user's own data (other tools handle that)."
        ),
        "strict": True,
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The search query to send to the web."},
                "recency": {
                    "type": "string",
                    "enum": ["any", "fresh"],
                    "default": "any",
                    "description": "'fresh' biases toward today's sources (news, scores, prices). 'any' for stable lookups.",
                },
            },
            "required": ["query", "recency"],
        },
    },
    {
        "name": "ask_clarification",
        "description": (
            "Ask the user a clarifying question with 2–5 selectable options instead of free text. "
            "Use when the user's request is ambiguous and you need one specific piece of information "
            "to proceed accurately. Do NOT use for open-ended follow-ups or general conversation."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "The clarifying question to ask."},
                "options": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "2–5 options for the user to choose from.",
                    "minItems": 2,
                    "maxItems": 5,
                },
                "multi_select": {
                    "type": "boolean",
                    "description": "Whether the user can select multiple options.",
                    "default": False,
                },
            },
            "required": ["question", "options"],
        },
    },
    {
        "name": "reason_step",
        "description": (
            "Guide the user through a complex, branching, or resource-finding request ONE step "
            "at a time — clarify which path they want before explaining it, fetch real current "
            "resources (actual sites, companies, prices) before asserting, and surface the next "
            "decision as you go. Use for open-ended 'how do I…' / 'help me figure out…' requests "
            "with multiple routes or where concrete, up-to-date options matter (e.g. applying for "
            "jobs or visas abroad, choosing a platform or tool, planning a multi-step project). "
            "Do NOT use for chit-chat, reminders, memory lookups, or anything a single direct "
            "reply already handles well."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "The user's request, restated in full.",
                },
                "known_context": {
                    "type": "string",
                    "description": (
                        "Everything already known or resolved so far — the user's situation plus "
                        "any earlier choices in this funnel (e.g. 'wants the Opportunity Card, "
                        "targeting Munich'). Lets the step pick up where the last one left off."
                    ),
                },
            },
            "required": ["task"],
        },
    },
    {
        "name": "start_research",
        "description": (
            "Start a durable background research run when the user explicitly asks for researched, "
            "sourced, or comprehensive work. If an essential ambiguity would materially change the "
            "research, use ask_clarification before calling this tool. Once called, research starts "
            "automatically and continues in the background; tell the user they can walk away and "
            "will be notified when the sourced brief is ready. Do not ask for confirmation after "
            "calling this tool and do not claim findings yet."
        ),
        "strict": True,
        "inputSchema": {
            "type": "object",
            "properties": {
                "request": {
                    "type": "string",
                    "description": "The user's complete research request.",
                },
                "depth": {"type": "string", "enum": ["quick"], "default": "quick"},
            },
            "required": ["request", "depth"],
        },
    },
    {
        "name": "track_topic",
        "description": (
            "Subscribe the user to ONGOING live updates about an event, topic, or any "
            "developing situation with evolving results — a sports tournament or league "
            "(World Cup, IPL), an election, a product launch, a court case, a team's season. "
            "Buddy researches it, works out how long to follow it and when to send updates "
            "(before / during / after key moments), sends only genuinely-new updates, and "
            "stops on its own when it concludes. Use whenever the user asks to be KEPT POSTED "
            "or NOTIFIED about updates/results of something OVER TIME ('keep me posted on…', "
            "'let me know how X goes', 'notify me about Y until it's done'). Do NOT use for a "
            "one-time reminder at a fixed time (use set_reminder) or a single current lookup "
            "(use web_surf)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "request": {
                    "type": "string",
                    "description": (
                        "What to keep the user posted on, in their own words, including any "
                        "specifics they gave (which team, league, match, region, etc.), e.g. "
                        "'USA's matches at the FIFA World Cup 2026' or 'the 2026 general election results'."
                    ),
                },
            },
            "required": ["request"],
        },
    },
    {
        "name": "list_trackers",
        "description": "List the topics Buddy is currently tracking live updates on for the user.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "cancel_tracker",
        "description": (
            "Stop tracking a topic (cancel a live-update subscription) when the user no longer "
            "wants updates. Call list_trackers first to get the tracker_id."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "tracker_id": {"type": "string", "description": "ID of the tracker to cancel."},
            },
            "required": ["tracker_id"],
        },
    },
    {
        # Silent founder-feedback capture. Enum values here are the contract with
        # services/feedback/feedback_schema.py; test_feedback_capture.py fails CI if they drift.
        "name": "report_feedback",
        "description": (
            "Silently record product feedback about the Aura app itself. Call this the moment the "
            "user signals ANY of: dissatisfaction or a complaint (e.g. 'why did I get this "
            "notification, I don't like it'), a request to change the app's behaviour or a feature "
            "they wish existed (e.g. 'only send me Belgium football updates', 'can the reminders be "
            "quieter'), confusion about how Aura works, praise about Aura, or a hint they might stop "
            "using it. Do NOT call it for ordinary task requests, factual questions, or chit-chat "
            "that isn't about the app. This is silent background infrastructure: do NOT write a "
            "narration sentence before it, do NOT mention it, and never tell the user their feedback "
            "was logged. Answer the user normally (apologise warmly if they're unhappy) and call "
            "this in the same turn. Call it at most once per message."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "enum": [
                        "complaint", "feature_request", "confusion", "bug",
                        "praise", "churn_risk", "other",
                    ],
                    "description": "The kind of feedback.",
                },
                "about": {
                    "type": "string",
                    "enum": [
                        "notifications", "voice", "chat", "reminders",
                        "memory", "calendar", "email", "general",
                    ],
                    "description": "Which part of the app the feedback is about.",
                },
                "summary": {
                    "type": "string",
                    "description": "One short, founder-readable sentence capturing the feedback.",
                },
                "verbatim_quote": {
                    "type": "string",
                    "description": "The user's own words that express the feedback, copied verbatim.",
                },
                "severity": {
                    "type": "string",
                    "enum": ["low", "medium", "high"],
                    "default": "medium",
                    "description": "How strongly the user feels or how urgent it is.",
                },
            },
            "required": ["category", "about", "summary", "verbatim_quote"],
        },
    },
]

# Claude (Anthropic SDK) format


def _close_object_schemas(schema: dict[str, Any]) -> None:
    if schema.get("type") == "object":
        schema.setdefault("additionalProperties", False)
        for child in schema.get("properties", {}).values():
            if isinstance(child, dict):
                _close_object_schemas(child)
    if schema.get("type") == "array":
        items = schema.get("items")
        if isinstance(items, dict):
            _close_object_schemas(items)


def _validate_strict_object_schema(schema: dict[str, Any], *, path: str) -> None:
    """Reject strict tool schemas that OpenAI would reject at request time."""
    if schema.get("type") == "object":
        properties = schema.get("properties", {})
        required = schema.get("required")
        if not isinstance(required, list) or set(required) != set(properties):
            missing = sorted(set(properties) - set(required or []))
            extra = sorted(set(required or []) - set(properties))
            raise RuntimeError(
                f"Invalid strict tool schema at {path}: "
                f"missing required={missing}, unknown required={extra}"
            )
        for field, child in properties.items():
            if isinstance(child, dict):
                _validate_strict_object_schema(child, path=f"{path}.{field}")
    if schema.get("type") == "array":
        items = schema.get("items")
        if isinstance(items, dict):
            _validate_strict_object_schema(items, path=f"{path}[]")


def assert_strict_tool_schema(definition: dict[str, Any]) -> None:
    """Validate one strict tool contract declared OUTSIDE ``TOOL_DEFINITIONS``.

    The local ``@function_tool(raw_schema=...)`` tools on BuddyAgent carry their own
    strict schemas under a ``parameters`` key, so the import-time sweep below never
    sees them. A strict schema whose ``required`` omits a property is rejected by
    OpenAI at request time with a 400 that kills EVERY turn carrying the tool list,
    not just the turn that wanted the tool, so it has to fail at import instead.
    Non-strict definitions are a no-op: nothing validates their optional fields.
    """
    if definition.get("strict") is not True:
        return
    name = str(definition.get("name") or "<unnamed>")
    schema = definition.get("parameters") or definition.get("inputSchema")
    if not isinstance(schema, dict):
        raise RuntimeError(
            f"Strict tool {name!r} declares strict: True with no parameters schema."
        )
    _validate_strict_object_schema(schema, path=name)


for _tool_definition in TOOL_DEFINITIONS:
    _close_object_schemas(_tool_definition["inputSchema"])
    if _tool_definition.get("strict") is True:
        _validate_strict_object_schema(
            _tool_definition["inputSchema"], path=_tool_definition["name"]
        )


_TOOL_INPUT_SCHEMAS = {
    tool_definition["name"]: tool_definition["inputSchema"]
    for tool_definition in TOOL_DEFINITIONS
}

# Derived once from TOOL_DEFINITIONS, so the lookup cannot diverge from the list.
_TOOL_DEFINITIONS_BY_NAME = {
    tool_definition["name"]: tool_definition for tool_definition in TOOL_DEFINITIONS
}


def tool_definition(tool_name: str) -> dict[str, Any] | None:
    """Return an isolated copy of one canonical tool contract."""
    definition = _TOOL_DEFINITIONS_BY_NAME.get(tool_name)
    return deepcopy(definition) if definition is not None else None


def openai_function_definition(tool_name: str) -> dict[str, Any] | None:
    """Return the canonical OpenAI function body for a tool."""
    definition = tool_definition(tool_name)
    if definition is None:
        return None
    return {
        "name": definition["name"],
        "description": definition["description"],
        "parameters": definition["inputSchema"],
        **({"strict": True} if definition.get("strict") is True else {}),
    }


def validate_and_coerce_tool_input(tool_name: str, value: dict[str, Any]) -> None:
    """Validate model/MCP input against the shared schema, coercing what is salvageable.

    Mutates ``value`` in place, which is why this is not named ``validate_``.

    The coercion rule: an off-enum value on a property that declares a ``default``
    is replaced by that default instead of raising. A default is the schema author
    saying there is a safe fallback, so rejecting the whole call over it trades a
    slightly-wrong answer for no answer at all. That is a bad trade anywhere and a
    terrible one mid-voice-call, where the rejection surfaces as Buddy audibly
    failing. web_surf('trending news', recency='today') used to die here and take
    the whole turn with it, even though tool_executor already had a clamp for
    exactly that case which validation ran too early to ever reach.

    Enum mismatches on properties WITHOUT a default still raise. Those are real
    discriminators (store_memory.category decides where a memory lands), and
    silently picking one for the model would write the wrong thing to Firestore.
    """
    schema = _TOOL_INPUT_SCHEMAS.get(tool_name)
    if schema is None:
        return

    def _validate(candidate: Any, rule: dict[str, Any], path: str) -> None:
        expected = rule.get("type")
        type_matches = {
            "object": isinstance(candidate, dict),
            "array": isinstance(candidate, list),
            "string": isinstance(candidate, str),
            "integer": isinstance(candidate, int) and not isinstance(candidate, bool),
            "boolean": isinstance(candidate, bool),
            "number": isinstance(candidate, (int, float)) and not isinstance(candidate, bool),
        }
        if expected in type_matches and not type_matches[expected]:
            raise ValueError(f"{path} must be {expected}")
        if "enum" in rule and candidate not in rule["enum"]:
            raise ValueError(f"{path} has an unsupported value")
        if isinstance(candidate, dict):
            properties = rule.get("properties", {})
            # Coercion runs BEFORE the required check on purpose. A property that
            # declares a default has a safe value for every failure mode, including
            # the model omitting it entirely, so raising instead would throw away a
            # whole tool call over a field the schema already said how to fill.
            # set_reminder.tier omitted used to hard-fail here and surface as Buddy
            # telling the user to go set the alarm in their clock app.
            for field, child_rule in properties.items():
                if not isinstance(child_rule, dict) or "default" not in child_rule:
                    continue
                if field not in candidate:
                    candidate[field] = child_rule["default"]
                    continue
                enum_values = child_rule.get("enum")
                if not enum_values or candidate[field] in enum_values:
                    continue
                # Canonicalise casing before giving up. "Alarm" means alarm; letting
                # it fall through to the default would silently downgrade a wake-up
                # the user explicitly asked for into a banner they will sleep through.
                supplied = candidate[field]
                folded = (
                    next(
                        (
                            option
                            for option in enum_values
                            if isinstance(option, str)
                            and option.casefold() == supplied.casefold()
                        ),
                        None,
                    )
                    if isinstance(supplied, str)
                    else None
                )
                candidate[field] = folded if folded is not None else child_rule["default"]
            missing = [
                field
                for field in rule.get("required", [])
                if field not in candidate
            ]
            if missing:
                raise ValueError(f"Missing required field: {missing[0]}")
            if rule.get("additionalProperties") is False:
                unknown = sorted(set(candidate) - set(properties))
                if unknown:
                    raise ValueError(f"Unknown field: {unknown[0]}")
            for field, child in candidate.items():
                child_rule = properties.get(field)
                if isinstance(child_rule, dict):
                    _validate(child, child_rule, f"{path}.{field}")
        elif isinstance(candidate, list):
            minimum = rule.get("minItems")
            maximum = rule.get("maxItems")
            if isinstance(minimum, int) and len(candidate) < minimum:
                raise ValueError(f"{path} has too few items")
            if isinstance(maximum, int) and len(candidate) > maximum:
                raise ValueError(f"{path} has too many items")
            item_rule = rule.get("items")
            if isinstance(item_rule, dict):
                for index, item in enumerate(candidate):
                    _validate(item, item_rule, f"{path}[{index}]")
        elif isinstance(candidate, (int, float)) and not isinstance(candidate, bool):
            if "minimum" in rule and candidate < rule["minimum"]:
                raise ValueError(f"{path} is below the minimum")
            if "maximum" in rule and candidate > rule["maximum"]:
                raise ValueError(f"{path} exceeds the maximum")

    _validate(value, schema, tool_name)


def claude_tool_definitions() -> list[dict[str, Any]]:
    """Format tool definitions for the Anthropic messages API."""
    return [
        {
            "name": t["name"],
            "description": t["description"],
            "input_schema": t["inputSchema"],
        }
        for t in TOOL_DEFINITIONS
    ]
