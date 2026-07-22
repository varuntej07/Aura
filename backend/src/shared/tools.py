"""
Tool definitions for the Claude text chat endpoint (Anthropic SDK format).
The LiveKit voice agent uses @function_tool decorated methods on BuddyAgent instead.
"""

from typing import Any

# Canonical tool specs

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "set_reminder",
        "description": "Schedule a reminder for the user at a specific date and time.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "What to remind the user about."},
                # Include the timezone offset so the server can normalize to UTC correctly.
                "scheduled_at": {
                    "type": "string",
                    "description": (
                        "When to send the reminder, as an ISO 8601 datetime string with timezone "
                        "offset (e.g. '2026-06-02T09:00:00+05:30'). Use the current date and "
                        "timezone from your system context to compute this."
                    ),
                },
                "priority": {
                    "type": "string",
                    "enum": ["low", "normal", "urgent"],
                    "default": "normal",
                },
            },
            "required": ["message", "scheduled_at"],
        },
    },
    {
        "name": "list_reminders",
        "description": "List the user's reminders.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "status_filter": {
                    "type": "string",
                    "enum": ["pending", "fired", "all"],
                    "default": "pending",
                },
            },
        },
    },
    {
        "name": "cancel_reminder",
        "description": "Cancel (dismiss) a pending reminder.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "reminder_id": {"type": "string", "description": "ID of the reminder to cancel."},
            },
            "required": ["reminder_id"],
        },
    },
    {
        "name": "create_calendar_event",
        "description": "Create an event on the user's Google Calendar.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "start_time": {"type": "string", "description": "ISO 8601 datetime string."},
                "end_time": {"type": "string", "description": "ISO 8601 datetime string. Defaults to 30 min after start."},
                "description": {"type": "string"},
                "location": {"type": "string"},
                "attendees": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Guest email addresses to invite. Google emails each one a calendar "
                        "invitation. Include only when the user names people to invite."
                    ),
                },
            },
            "required": ["title", "start_time"],
        },
    },
    {
        "name": "get_upcoming_events",
        "description": (
            "Retrieve the user's cached Google Calendar events. "
            "Use whenever the user asks about their schedule, meetings, "
            "appointments, or what they have today, tomorrow, or this week. "
            "Prefer range='today', range='tomorrow', or range='this_week'. "
            "Use custom start_time/end_time only when the user gives an explicit time range."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "range": {
                    "type": "string",
                    "description": (
                        "Named range interpreted in the connected calendar's timezone."
                    ),
                    "enum": ["today", "tomorrow", "this_week"],
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
    # list_emails / read_email are disabled: they require the gmail.readonly restricted
    # scope, which would force the OAuth app into an annual paid CASA security assessment.
    # Only send_email (gmail.send, a free "sensitive" scope) is exposed. Restore these two
    # definitions together with GMAIL_READONLY_SCOPE in gmail_connector.py if you take on CASA.
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
        "description": "Persist a fact, preference, or habit about the user for future context.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "Semantic key, e.g. 'bedtime'."},
                "value": {"type": "string", "description": "Value to store."},
                "category": {
                    "type": "string",
                    "enum": ["preferences", "facts", "habits", "health", "routines"],
                },
            },
            "required": ["key", "value", "category"],
        },
    },
    {
        "name": "query_memory",
        "description": "Search the user's stored memories.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search string."},
                "category_filter": {
                    "type": "string",
                    "enum": ["preferences", "facts", "habits", "health", "routines", "all"],
                    "default": "all",
                },
            },
            "required": ["query"],
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
            "required": ["query"],
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
