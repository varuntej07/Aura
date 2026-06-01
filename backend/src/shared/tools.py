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
    {
        "name": "list_emails",
        "description": (
            "List the user's recent Gmail messages (sender, subject, date, snippet). "
            "Use when the user asks about their inbox, recent emails, or wants to find a "
            "specific message. Returns message ids to pass to read_email for full content."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Optional Gmail search query (same syntax as the Gmail search box, "
                        "e.g. 'from:sam is:unread newer_than:7d'). Omit for the most recent messages."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "default": 10,
                    "minimum": 1,
                    "maximum": 25,
                },
            },
        },
    },
    {
        "name": "read_email",
        "description": (
            "Read the full body of one Gmail message by its id. "
            "Call list_emails first to get the message id."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "message_id": {"type": "string", "description": "Gmail message id from list_emails."},
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
