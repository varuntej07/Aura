"""Dynamic voice context appended after a stable surface-specific prompt."""

from __future__ import annotations


VOICE_SESSION_CONTEXT_START = "<session>"


def render_voice_session_context(context_vars: dict[str, str]) -> str:
    """Render every per-session value once, after all stable instructions."""
    return f"""
            {VOICE_SESSION_CONTEXT_START}
            Background. Latest finalized user turn has authority.
            User: {context_vars["name"]}
            Local: {context_vars["local_time"]}, {context_vars["local_date"]},
            {context_vars["timezone"]}
            Archive:
            {context_vars["archive_context"]}
            Aura profile:
            {context_vars["user_aura_profile"]}
            Last session:
            {context_vars["last_session_context"]}
            Recent memory:
            {context_vars["memory_summary"]}{context_vars["graph_context"]}
            </session>
"""
