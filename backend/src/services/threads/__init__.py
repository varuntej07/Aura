"""Open-loop thread engine.

Tracks holes in what Buddy knows about the user (v1: from reminders) and, on a
slow cadence, asks one warm, curious question to fill the most interesting hole.
The answer flows back through the normal chat path and enriches UserAura.
"""
