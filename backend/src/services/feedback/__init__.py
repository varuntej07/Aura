"""Silent product-feedback capture for Buddy's `report_feedback` tool.

A user giving feedback mid-conversation (a complaint, a feature request, confusion, praise, or a
churn signal) is detected by the chat/voice model itself, which calls `report_feedback`. The
structured arguments are persisted to the `observed_feedback` Firestore collection and pinged to the
founder's Telegram. See `feedback_schema` for the taxonomies/field names, `feedback_capture` for the
orchestration, and `telegram_client` for the best-effort alert transport.
"""
